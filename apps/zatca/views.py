"""
Admin API for ZATCA e-invoicing — /api/admin/zatca/

  GET   status/                         settings, device, counts (managers can look; only admins change)
  PATCH settings/                       seller details, VAT rate, the on/off switch
  POST|DELETE logo/                     the logo printed on receipts (multipart `logo`)
  POST  onboard/start/                  go-live wizard: new device for an environment
  POST  onboard/<id>/otp/               … compliance certificate from the Fatoora OTP
  POST  onboard/<id>/checks/            … the three compliance checks
  POST  onboard/<id>/production/        … production certificate
  POST  onboard/<id>/activate/          … this device issues every invoice from now on
  GET   documents/                      invoice log (?status= ?type= ?q= &page=)
  GET   documents/<id>/                 one document with ZATCA's messages
  GET   documents/<id>/xml/             the signed XML
  POST  documents/<id>/send/            report it now
  POST  retry/                          resend everything that is due
  GET   events/                         audit log
  GET|POST cron/                        the retry job for a scheduler (X-Cron-Secret or Bearer token)
  GET|POST companies/                   members' saved companies (B2B buyers) · ?q= ?member=
  PATCH|DELETE companies/<id>/

Members manage their own companies at /api/zatca/companies/ (member_views below).

Secrets never leave the server: the device shows "set / not set", dates and certificate details only.
"""
import hmac
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response

from . import crypto, environments, service
from .models import BusinessCustomer, ZatcaConfig, ZatcaDevice, ZatcaDocument, ZatcaEvent

SETTINGS_FIELDS = ['seller_name_ar', 'seller_name_en', 'vat_number', 'crn', 'street', 'building_number',
                   'additional_number', 'district', 'city', 'postal_code', 'branch_name', 'business_category',
                   'vat_rate', 'address_ar']


class IsGymAdmin(BasePermission):
    """Reading: admins and managers. Changing anything: admins only."""
    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated and u.role in ('admin', 'manager')):
            return False
        return request.method in ('GET', 'HEAD', 'OPTIONS') or u.role == 'admin'


# ---------------------------------------------------------------- rows
def _device_row(d):
    if d is None:
        return None
    days = (d.certificate_expires_at - timezone.now()).days if d.certificate_expires_at else None
    results = d.compliance_results or {}
    return {
        'id': d.id, 'environment': d.environment, 'environment_label': environments.get(d.environment)['label'],
        'status': d.status, 'common_name': d.common_name, 'serial': d.serial,
        'private_key': 'set' if d.private_key_enc else 'not set',
        'compliance_certificate': 'set' if d.compliance_token_enc else 'not set',
        'production_certificate': 'set' if d.production_token_enc else 'not set',
        'compliance_checks': results, 'invoice_types': d.invoice_types, 'b2b': d.issues_b2b,
        'checks_passed': len(results) >= service._expected_checks(d) and all(v.get('ok') for v in results.values()),
        'certificate_issuer': d.certificate_issuer, 'certificate_expires_at': d.certificate_expires_at,
        'certificate_days_left': days, 'certificate_expiring': days is not None and days <= 30,
        'last_icv': d.last_icv, 'created_at': d.created_at, 'activated_at': d.activated_at,
        'archived_at': d.archived_at,
    }


def _doc_row(doc, full=False):
    row = {
        'id': doc.id, 'number': doc.number, 'doc_type': doc.doc_type, 'doc_type_label': doc.get_doc_type_display(),
        'icv': doc.icv, 'issued_at': doc.issued_at, 'customer_name': doc.customer_name,
        'net': doc.net, 'vat': doc.vat, 'total': doc.total, 'status': doc.status,
        'status_label': doc.get_status_display(), 'attempts': doc.attempts, 'next_attempt_at': doc.next_attempt_at,
        'last_error': doc.last_error, 'has_warnings': bool(doc.zatca_warnings), 'reported_at': doc.reported_at,
        'environment': doc.device.environment, 'test': doc.is_test, 'standard': doc.is_standard,
        'buyer': doc.buyer_details.get('name', ''),
        'original': doc.original.number if doc.original_id else '',
        'late': doc.status == 'pending' and doc.issued_at < timezone.now() - service.LATE_AFTER,
    }
    if full:
        row.update({
            'uuid': str(doc.uuid), 'invoice_hash': doc.invoice_hash, 'pih': doc.pih, 'qr': doc.qr,
            'lines': doc.lines, 'rounding': doc.rounding, 'vat_rate': doc.vat_rate,
            'payment_label': doc.payment_label, 'reason': doc.reason,
            'warnings': doc.zatca_warnings, 'errors': doc.zatca_errors,
            'transaction_id': doc.transaction.transaction_id if doc.transaction_id else '',
            'registration_id': doc.registration_id, 'buyer_details': doc.buyer_details,
            'has_cleared_copy': bool(doc.cleared_xml),
            'credit_notes': [{'id': n.id, 'number': n.number, 'total': n.total, 'status': n.status}
                             for n in doc.notes.all()],
        })
    return row


def logo_url(cfg):
    try:
        return cfg.logo.url if cfg.logo else ''
    except Exception:   # noqa: BLE001 — storage not reachable: the receipt prints without it
        return ''


def _status_payload():
    cfg = service.config()
    device = service.active_device(cfg)
    onboarding = ZatcaDevice.objects.filter(status='onboarding').first()
    return {
        'settings': {f: getattr(cfg, f) for f in SETTINGS_FIELDS},
        'logo_url': logo_url(cfg),
        'enabled': cfg.enabled, 'enabled_at': cfg.enabled_at, 'environment': cfg.environment,
        'environment_label': environments.get(cfg.environment)['label'], 'test_mode': cfg.is_test,
        'locked_fields': service.locked_fields(cfg), 'problems': service.validate_seller(cfg),
        'encryption_key_set': crypto.key_configured(), 'cron_secret_set': bool(settings.ZATCA_CRON_SECRET),
        'live': service.is_on(cfg), 'b2b': service.b2b_on(cfg),
        'device': _device_row(device), 'onboarding': _device_row(onboarding),
        'archived_devices': [_device_row(d) for d in ZatcaDevice.objects.filter(status='archived')[:10]],
        'summary': service.summary(),
        'environments': [{'value': k, 'label': v['label'], 'legal': v['legal']}
                         for k, v in environments.ENVIRONMENTS.items()],
        'sandbox_otp': environments.SANDBOX_OTP, 'sandbox_vat': environments.SANDBOX_VAT,
    }


# ---------------------------------------------------------------- settings
@api_view(['GET'])
@permission_classes([IsGymAdmin])
def status(request):
    return Response(_status_payload())


@api_view(['PATCH'])
@permission_classes([IsGymAdmin])
def update_settings(request):
    cfg = service.config()
    locked = service.locked_fields(cfg)
    errors = {}
    for f in SETTINGS_FIELDS:
        if f not in request.data:
            continue
        value = request.data[f]
        value = str(value).strip() if value is not None else ''
        if f in locked and value != str(getattr(cfg, f)):
            errors[f] = 'Locked while this device is live — its certificate carries this value. Go live again to change it.'
            continue
        if f == 'vat_rate':
            try:
                value = Decimal(value or '15')
            except InvalidOperation:
                errors[f] = 'Enter a number, e.g. 15.'
                continue
        setattr(cfg, f, value)
    if not errors:
        errors.update(service.validate_seller(cfg))
    if 'enabled' in request.data:
        want = str(request.data['enabled']).lower() in ('1', 'true', 'yes', 'on')
        if want and service.active_device(cfg) is None:
            errors['enabled'] = 'Finish the go-live wizard first — there is no active device.'
        elif want and not cfg.enabled:
            cfg.enabled, cfg.enabled_at = True, timezone.now()
        elif not want:
            cfg.enabled = False
    if errors:
        return Response(errors, status=400)
    old_rate = ZatcaConfig.objects.get(pk=cfg.pk).vat_rate
    cfg.save()
    service.log('settings_changed', actor=request.user, fields=[f for f in SETTINGS_FIELDS + ['enabled'] if f in request.data])
    out = _status_payload()
    if cfg.vat_rate != 15 and cfg.vat_rate != old_rate:
        out['warning'] = 'The Saudi VAT rate is 15%. Change it only if your tax advisor told you to.'
    return Response(out)


@api_view(['POST', 'DELETE'])
@permission_classes([IsGymAdmin])
def logo(request):
    """The logo printed at the top of receipts. POST multipart `logo` (PNG/JPG/WebP, up to 1 MB); DELETE removes it."""
    cfg = service.config()
    old = cfg.logo.name if cfg.logo else None
    if request.method == 'DELETE':
        cfg.logo = None
    else:
        field = serializers.ImageField()
        try:
            image = field.run_validation(request.data.get('logo'))
        except (serializers.ValidationError, DjangoValidationError):
            return Response({'logo': ['Choose a PNG, JPG or WebP image.']}, status=400)
        if image.size > 1024 * 1024:
            return Response({'logo': ['The logo must be 1 MB or smaller.']}, status=400)
        cfg.logo = image
    cfg.save(update_fields=['logo', 'updated_at'])
    if old and old != (cfg.logo.name if cfg.logo else None):
        try:
            cfg.logo.storage.delete(old)
        except Exception:   # noqa: BLE001 — a leftover file is harmless
            pass
    service.log('settings_changed', actor=request.user, fields=['logo'])
    return Response(_status_payload())


# ---------------------------------------------------------------- go-live wizard
def _step(fn):
    try:
        return fn()
    except service.OnboardingError as exc:
        return Response({'detail': str(exc), **_status_payload()}, status=400)


def _onboarding_device(device_id):
    return get_object_or_404(ZatcaDevice, pk=device_id, status='onboarding')


@api_view(['POST'])
@permission_classes([IsGymAdmin])
def onboard_start(request):
    env = request.data.get('environment') or 'sandbox'
    if environments.ENVIRONMENTS.get(env, {}).get('legal') and request.data.get('confirm_production') is not True:
        return Response({'detail': 'Production invoices are legally real. Confirm to continue.'}, status=400)

    def go():
        service.start_device(env, actor=request.user, b2b=request.data.get('b2b') is True)
        return Response(_status_payload(), status=201)
    return _step(go)


@api_view(['POST'])
@permission_classes([IsGymAdmin])
def onboard_otp(request, device_id):
    device = _onboarding_device(device_id)
    return _step(lambda: (service.request_compliance(device, request.data.get('otp'), actor=request.user),
                          Response(_status_payload()))[1])


@api_view(['POST'])
@permission_classes([IsGymAdmin])
def onboard_checks(request, device_id):
    device = _onboarding_device(device_id)
    return _step(lambda: (service.run_compliance_checks(device, actor=request.user), Response(_status_payload()))[1])


@api_view(['POST'])
@permission_classes([IsGymAdmin])
def onboard_production(request, device_id):
    device = _onboarding_device(device_id)
    return _step(lambda: (service.request_production(device, actor=request.user), Response(_status_payload()))[1])


@api_view(['POST'])
@permission_classes([IsGymAdmin])
def onboard_activate(request, device_id):
    device = _onboarding_device(device_id)
    return _step(lambda: (service.activate(device, actor=request.user), Response(_status_payload()))[1])


# ---------------------------------------------------------------- documents
@api_view(['GET'])
@permission_classes([IsGymAdmin])
def documents(request):
    qs = ZatcaDocument.objects.select_related('device', 'original').order_by('-issued_at', '-id')
    st = request.query_params.get('status')
    if st == 'late':
        qs = qs.filter(status='pending', issued_at__lt=timezone.now() - service.LATE_AFTER)
    elif st == 'warnings':
        qs = qs.filter(status__in=['reported', 'cleared']).exclude(zatca_warnings=[])
    elif st == 'company':
        qs = qs.exclude(buyer_details={})
    elif st in ('pending', 'reported', 'cleared', 'rejected'):
        qs = qs.filter(status=st)
    if request.query_params.get('type') in ('invoice', 'credit', 'debit'):
        qs = qs.filter(doc_type=request.query_params['type'])
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(customer_name__icontains=q)
                       | Q(transaction__transaction_id__icontains=q) | Q(buyer_details__vat_number=q))
    paginator = PageNumberPagination()
    paginator.page_size = 25
    page = paginator.paginate_queryset(qs, request)
    return paginator.get_paginated_response([_doc_row(d) for d in page])


@api_view(['GET'])
@permission_classes([IsGymAdmin])
def document(request, doc_id):
    doc = get_object_or_404(ZatcaDocument.objects.select_related('device', 'original', 'transaction'), pk=doc_id)
    return Response(_doc_row(doc, full=True))


@api_view(['GET'])
@permission_classes([IsGymAdmin])
def document_xml(request, doc_id):
    doc = get_object_or_404(ZatcaDocument, pk=doc_id)
    cleared = request.query_params.get('copy') == 'cleared' and doc.cleared_xml
    resp = HttpResponse(doc.cleared_xml if cleared else doc.signed_xml, content_type='application/xml; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{doc.number}{"-cleared" if cleared else ""}.xml"'
    return resp


@api_view(['POST'])
@permission_classes([IsGymAdmin])
def document_send(request, doc_id):
    doc = get_object_or_404(ZatcaDocument, pk=doc_id)
    if doc.status != 'pending':
        return Response({'detail': f'This document is already {doc.get_status_display().lower()}.'}, status=400)
    doc = service.submit(doc.id)
    return Response(_doc_row(ZatcaDocument.objects.select_related('device', 'original').get(pk=doc.id), full=True))


@api_view(['POST'])
@permission_classes([IsGymAdmin])
def retry(request):
    return Response({**service.retry_due(), 'summary': service.summary()})


@api_view(['GET'])
@permission_classes([IsGymAdmin])
def events(request):
    rows = ZatcaEvent.objects.select_related('actor', 'device')[:100]
    return Response([{'id': e.id, 'action': e.action, 'detail': e.detail, 'created_at': e.created_at,
                      'actor': (e.actor.full_name or e.actor.email) if e.actor_id else 'System',
                      'device': e.device.common_name if e.device_id else ''} for e in rows])


# ---------------------------------------------------------------- cron
@api_view(['GET', 'POST'])
@authentication_classes([])          # the scheduler's Bearer token is the cron secret, not a JWT
@permission_classes([AllowAny])
def cron(request):
    """The retry job for an external scheduler. Without ZATCA_CRON_SECRET set this endpoint doesn't exist."""
    secret = settings.ZATCA_CRON_SECRET
    if not secret:
        raise Http404
    given = request.headers.get('X-Cron-Secret') or ''
    auth = request.headers.get('Authorization') or ''
    if auth.startswith('Bearer '):
        given = given or auth[7:]
    if not hmac.compare_digest(given.encode(), secret.encode()):
        return Response({'detail': 'Not allowed.'}, status=403)
    return Response(service.retry_due())


# ---------------------------------------------------------------- companies (B2B buyers)
def _company_data(data):
    return {f: str(data.get(f) or '').strip() for f in BusinessCustomer.FIELDS}


def _save_company(company, data):
    values = {**company.snapshot(), **{k: v for k, v in _company_data(data).items() if k in data}}
    errors = service.validate_company(values)
    if errors:
        return Response(errors, status=400)
    for f, v in values.items():
        setattr(company, f, v)
    company.save()
    return None


@api_view(['GET', 'POST'])
@permission_classes([IsGymAdmin])
def companies(request):
    if request.method == 'POST':
        from django.contrib.auth import get_user_model
        owner = get_user_model().objects.filter(pk=request.data.get('owner_id'), role='member').first() \
            if str(request.data.get('owner_id') or '').isdigit() else None
        if owner is None:
            return Response({'owner_id': 'Choose the member this company belongs to.'}, status=400)
        company = BusinessCustomer(owner=owner)
        error = _save_company(company, request.data)
        if error:
            return error
        service.log('company_added', actor=request.user, company=company.name, member=owner.email)
        return Response(service.company_row(company), status=201)
    qs = BusinessCustomer.objects.select_related('owner').filter(is_active=True)
    if str(request.query_params.get('member') or '').isdigit():
        qs = qs.filter(owner_id=request.query_params['member'])
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(vat_number__icontains=q) | Q(owner__email__icontains=q)
                       | Q(owner__first_name__icontains=q) | Q(owner__last_name__icontains=q))
    return Response([service.company_row(c) for c in qs[:200]])


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsGymAdmin])
def company(request, company_id):
    c = get_object_or_404(BusinessCustomer.objects.select_related('owner'), pk=company_id, is_active=True)
    if request.method == 'DELETE':
        c.is_active = False                          # invoices keep their frozen copy of the details
        c.save(update_fields=['is_active'])
        return Response(status=204)
    return _save_company(c, request.data) or Response(service.company_row(c))


# ---------------------------------------------------------------- the member's own companies
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def my_companies(request):
    """GET → {b2b, companies}: `b2b` false = company invoices aren't offered at checkout."""
    if request.method == 'POST':
        if not service.b2b_on():
            return Response({'detail': 'Company tax invoices aren’t available right now.'}, status=400)
        c = BusinessCustomer(owner=request.user)
        return _save_company(c, request.data) or Response(service.company_row(c), status=201)
    rows = BusinessCustomer.objects.filter(owner=request.user, is_active=True)
    return Response({'b2b': service.b2b_on(), 'companies': [service.company_row(c) for c in rows]})


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def my_company(request, company_id):
    c = get_object_or_404(BusinessCustomer, pk=company_id, owner=request.user, is_active=True)
    if request.method == 'DELETE':
        c.is_active = False
        c.save(update_fields=['is_active'])
        return Response(status=204)
    return _save_company(c, request.data) or Response(service.company_row(c))
