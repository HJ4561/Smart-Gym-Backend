"""
Admin API for ZATCA e-invoicing — /api/admin/zatca/

  GET   status/                         settings, device, counts (managers can look; only admins change)
  PATCH settings/                       seller details, VAT rate, the on/off switch
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

Secrets never leave the server: the device shows "set / not set", dates and certificate details only.
"""
import hmac
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response

from . import crypto, environments, service
from .models import ZatcaConfig, ZatcaDevice, ZatcaDocument, ZatcaEvent

SETTINGS_FIELDS = ['seller_name_ar', 'seller_name_en', 'vat_number', 'crn', 'street', 'building_number',
                   'additional_number', 'district', 'city', 'postal_code', 'branch_name', 'business_category',
                   'vat_rate']


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
        'compliance_checks': results,
        'checks_passed': len(results) >= 3 and all(v.get('ok') for v in results.values()),
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
        'environment': doc.device.environment, 'test': doc.is_test,
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
            'registration_id': doc.registration_id,
            'credit_notes': [{'id': n.id, 'number': n.number, 'total': n.total, 'status': n.status}
                             for n in doc.notes.all()],
        })
    return row


def _status_payload():
    cfg = service.config()
    device = service.active_device(cfg)
    onboarding = ZatcaDevice.objects.filter(status='onboarding').first()
    return {
        'settings': {f: getattr(cfg, f) for f in SETTINGS_FIELDS},
        'enabled': cfg.enabled, 'enabled_at': cfg.enabled_at, 'environment': cfg.environment,
        'environment_label': environments.get(cfg.environment)['label'], 'test_mode': cfg.is_test,
        'locked_fields': service.locked_fields(cfg), 'problems': service.validate_seller(cfg),
        'encryption_key_set': crypto.key_configured(), 'cron_secret_set': bool(settings.ZATCA_CRON_SECRET),
        'live': service.is_on(cfg), 'device': _device_row(device), 'onboarding': _device_row(onboarding),
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
        service.start_device(env, actor=request.user)
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
        qs = qs.filter(status='reported').exclude(zatca_warnings=[])
    elif st in ('pending', 'reported', 'rejected'):
        qs = qs.filter(status=st)
    if request.query_params.get('type') in ('invoice', 'credit', 'debit'):
        qs = qs.filter(doc_type=request.query_params['type'])
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(customer_name__icontains=q)
                       | Q(transaction__transaction_id__icontains=q))
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
    resp = HttpResponse(doc.signed_xml, content_type='application/xml; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="{doc.number}.xml"'
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
