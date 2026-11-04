"""
ZATCA e-invoicing, end to end:

  issue()           build + sign one document on the active device's chain (ICV + PIH, row-locked)
  submit()          report it to ZATCA; failures wait and are retried, never block a payment
  retry_due()       the retry job (cron / Task Scheduler / the admin page)
  on_sale() …       the hooks payments call: a tax invoice for every paid sale, a credit note for
                    every refund of an invoiced sale. Locker top-ups are not invoiced — the credit
                    is invoiced when it is spent.
  onboarding        new device → compliance certificate (OTP) → compliance checks → production
                    certificate → active

Nothing here raises into a payment: a ZATCA problem is logged and shown on the admin page.
"""
import logging
import threading
import uuid
from datetime import UTC, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections, transaction as db_transaction
from django.utils import timezone

from . import client, crypto, csr as csr_mod, environments, signer, vat, xml_builder as X
from .models import ZatcaConfig, ZatcaDevice, ZatcaDocument, ZatcaEvent

logger = logging.getLogger(__name__)

RETRY_MINUTES = [5, 15, 60, 180, 360, 720]       # then every 12 hours
LATE_AFTER = timedelta(hours=12)                 # not reported after this → the admin page warns
PREFIX = {'invoice': 'INV', 'credit': 'CRN', 'debit': 'DBN'}


# ============================================================
# STATE
# ============================================================
def config():
    return ZatcaConfig.load()


def active_device(cfg=None):
    cfg = cfg or config()
    return ZatcaDevice.objects.filter(status='active', environment=cfg.environment).first()


def is_on(cfg=None):
    cfg = cfg or config()
    return cfg.enabled and crypto.key_configured() and active_device(cfg) is not None


def after_commit(fn, *args):
    """Run a hook once the payment's transaction commits; a ZATCA error never reaches the payment."""
    def run():
        try:
            fn(*args)
        except Exception:   # noqa: BLE001 — e.g. tables not migrated yet; the payment already stands
            logger.exception('ZATCA hook %s%r failed', fn.__name__, args)
    db_transaction.on_commit(run)


def log(action, *, device=None, actor=None, **detail):
    ZatcaEvent.objects.create(action=action, device=device,
                              actor=actor if getattr(actor, 'is_authenticated', False) else None, detail=detail)


def payment_means(label):
    text = (label or '').lower()
    if 'cash' in text:
        return X.CASH
    if 'locker' in text or 'credit:' in text:
        return X.NOT_DEFINED
    return X.CARD


# ============================================================
# ISSUE
# ============================================================
def issue(doc_type, items, *, payment_label='', customer_name='', transaction=None, registration=None,
          original=None, reason=''):
    """Build, sign and store one document; returns it, or None when ZATCA is off or it failed."""
    cfg = config()
    if not is_on(cfg):
        return None
    try:
        with db_transaction.atomic():
            doc = _issue(cfg, doc_type, items, payment_label, customer_name, transaction, registration, original, reason)
    except Exception:   # noqa: BLE001 — never break the payment that triggered it
        logger.exception('ZATCA: could not issue a %s', doc_type)
        return None
    if doc is not None:
        db_transaction.on_commit(lambda: send_soon(doc.id))
    return doc


def _issue(cfg, doc_type, items, payment_label, customer_name, transaction, registration, original, reason):
    device = ZatcaDevice.objects.select_for_update().get(pk=active_device(cfg).pk)
    breakdown = vat.split(items, cfg.vat_rate)
    if breakdown.paid <= 0:
        return None
    icv = device.last_icv + 1
    issued = timezone.localtime().replace(microsecond=0)
    number = f'SG{device.id}-{PREFIX[doc_type]}-{icv:06d}'
    doc_uuid = str(uuid.uuid4())
    spec = X.Document(doc_type, number, doc_uuid, issued, icv, device.last_hash, cfg.seller(), breakdown,
                      payment_means=payment_means(payment_label),
                      billing_reference=original.number if original else '', reason=reason[:200])
    signed = _sign(device, cfg, spec, breakdown, issued, production=True)
    doc = ZatcaDocument.objects.create(
        device=device, doc_type=doc_type, number=number, uuid=doc_uuid, icv=icv, issued_at=issued,
        pih=device.last_hash, invoice_hash=signed.invoice_hash, signed_xml=signed.xml, qr=signed.qr,
        lines=[{'name': l.name, 'qty': l.quantity, 'unit_net': str(vat.to_sar(l.unit_net)),
                'net': str(vat.to_sar(l.net)), 'vat': str(vat.to_sar(l.vat))} for l in breakdown.lines],
        net=vat.to_sar(breakdown.net), vat=vat.to_sar(breakdown.vat), rounding=vat.to_sar(breakdown.rounding),
        total=vat.to_sar(breakdown.paid), vat_rate=cfg.vat_rate, payment_means=spec.payment_means,
        payment_label=(payment_label or '')[:60], customer_name=(customer_name or '')[:150],
        transaction=transaction, registration=registration, original=original, reason=reason[:200],
    )
    device.last_icv, device.last_hash = icv, signed.invoice_hash
    device.save(update_fields=['last_icv', 'last_hash'])
    return doc


def _sign(device, cfg, spec, breakdown, issued, *, production):
    cert = signer.Certificate.from_body(device.certificate if production else _compliance_cert(device).body)
    key = csr_mod.load_private_key(crypto.decrypt(device.private_key_enc))
    return signer.sign(
        X.build(spec), private_key=key, certificate=cert, seller_name=cfg.seller_name, vat_number=cfg.vat_number,
        timestamp=issued.strftime('%Y-%m-%dT%H:%M:%S'), total=f'{vat.to_sar(breakdown.paid):.2f}',
        vat=f'{vat.to_sar(breakdown.vat):.2f}',
        signing_time=timezone.now().astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S'),
    )


# ============================================================
# SEND
# ============================================================
def send_soon(doc_id):
    """Report right after the payment commits — in a background thread, or inline when configured."""
    if getattr(settings, 'ZATCA_SEND_INLINE', False):
        submit(doc_id)
        return

    def run():
        try:
            submit(doc_id, timeout=10)
        except Exception:   # noqa: BLE001 — the retry job picks it up
            logger.exception('ZATCA: background report of document %s failed', doc_id)
        finally:
            close_old_connections()
    threading.Thread(target=run, daemon=True).start()


def _backoff(attempts):
    minutes = RETRY_MINUTES[attempts - 1] if attempts <= len(RETRY_MINUTES) else RETRY_MINUTES[-1]
    return timezone.now() + timedelta(minutes=minutes)


def submit(doc_id, timeout=client.TIMEOUT):
    """Report one pending document. Returns it (status updated)."""
    with db_transaction.atomic():
        doc = ZatcaDocument.objects.select_for_update().select_related('device').get(pk=doc_id)
        if doc.status != 'pending':
            return doc
        device = doc.device
        result = client.report(device.environment, crypto.decrypt(device.production_token_enc),
                               crypto.decrypt(device.production_secret_enc), doc.invoice_hash, str(doc.uuid),
                               doc.signed_xml, timeout=timeout)
        doc.attempts += 1
        if result.ok:
            doc.status, doc.reported_at = 'reported', timezone.now()
            doc.zatca_warnings, doc.zatca_errors, doc.last_error, doc.next_attempt_at = result.warnings, [], '', None
        elif result.status == 400 and result.validation.get('errorMessages'):
            # Rejected: it stays in the chain (numbers and hashes never get gaps) and is shown to the admin.
            doc.status, doc.zatca_errors, doc.zatca_warnings = 'rejected', result.errors, result.warnings
            doc.last_error, doc.next_attempt_at = (result.errors[0] if result.errors else 'Rejected')[:500], None
            logger.error('ZATCA rejected %s: %s', doc.number, result.errors)
        else:
            doc.last_error = (result.errors[0] if result.errors else f'HTTP {result.status}')[:500]
            doc.next_attempt_at = _backoff(doc.attempts)
        doc.save()
    if doc.status == 'rejected':
        log('document_rejected', device=doc.device, number=doc.number, errors=doc.zatca_errors[:5])
    return doc


def due_documents():
    now = timezone.now()
    return (ZatcaDocument.objects.filter(status='pending', device__status__in=['active', 'archived'])
            .exclude(next_attempt_at__gt=now).order_by('device_id', 'icv'))


def retry_due(limit=100):
    """The retry job: resend what is due. Returns {sent, reported, waiting, rejected}."""
    out = {'sent': 0, 'reported': 0, 'waiting': 0, 'rejected': 0}
    for doc_id in list(due_documents().values_list('id', flat=True)[:limit]):
        doc = submit(doc_id)
        out['sent'] += 1
        out[{'reported': 'reported', 'rejected': 'rejected'}.get(doc.status, 'waiting')] += 1
    return out


def summary():
    qs = ZatcaDocument.objects.all()
    return {
        'pending': qs.filter(status='pending').count(),
        'late': qs.filter(status='pending', issued_at__lt=timezone.now() - LATE_AFTER).count(),
        'rejected': qs.filter(status='rejected').count(),
        'reported': qs.filter(status='reported').count(),
        'with_warnings': qs.filter(status='reported').exclude(zatca_warnings=[]).count(),
    }


# ============================================================
# HOOKS (payments → documents)
# ============================================================
def _sale_items(txn):
    from apps.payments.models import PaymentIntent
    intent = PaymentIntent.objects.select_related('order').filter(transaction=txn).first()
    order = getattr(intent, 'order', None)
    if order is not None:
        items = list(order.items.all())
        base = sum((Decimal(it.line_total) for it in items), Decimal(0))
        if items and base > 0:
            # Spread what was actually paid (discount and VAT included) over the items.
            out, left = [], vat.to_halalas(txn.amount)
            for i, it in enumerate(items):
                share = left if i == len(items) - 1 else vat.to_halalas(Decimal(txn.amount) * Decimal(it.line_total) / base)
                left -= share
                out.append((it.product_name, it.quantity, vat.to_sar(share)))
            return out
    return [(txn.description, 1, txn.amount)]


def on_sale(txn_id):
    """A tax invoice for a paid sale (not top-ups, not refunds). Safe to call twice."""
    from apps.payments.models import Transaction
    cfg = config()
    txn = Transaction.objects.select_related('member').filter(pk=txn_id).first()
    if (txn is None or not is_on(cfg) or txn.amount <= 0 or txn.transaction_type in ('topup', 'refund')
            or (cfg.enabled_at and (txn.settled_at or txn.created_at) < cfg.enabled_at)
            or ZatcaDocument.objects.filter(transaction=txn, doc_type='invoice').exists()):
        return None
    return issue('invoice', _sale_items(txn), payment_label=txn.payment_method_label,
                 customer_name=txn.member.full_name or txn.member.email, transaction=txn)


def on_refund(original_txn_id, refund_txn_id, reason=''):
    """A credit note for a refund of an invoiced sale (sales from before ZATCA have no invoice)."""
    from apps.payments.models import Transaction
    original = ZatcaDocument.objects.filter(transaction_id=original_txn_id, doc_type='invoice').first()
    refund = Transaction.objects.select_related('member').filter(pk=refund_txn_id).first()
    if original is None or refund is None or not is_on():
        return None
    amount = min(abs(Decimal(refund.amount)), Decimal(original.total))
    return issue('credit', [(refund.description, 1, amount)], payment_label=refund.payment_method_label,
                 customer_name=original.customer_name, transaction=refund, original=original,
                 reason=reason or refund.description)


def signup_items(reg):
    facilities = reg.facilities or []
    plan_part = Decimal(reg.payment_amount) - sum((Decimal(str(f['amount'])) for f in facilities), Decimal(0))
    items = [(f'{reg.plan.display_name} membership × {reg.duration_months} months', 1, plan_part)]
    items += [(f"{f['name']} × {f['months']} months", 1, Decimal(str(f['amount']))) for f in facilities]
    return [i for i in items if Decimal(i[2]) > 0]


def on_signup_paid(reg_id):
    """A tax invoice for a website signup as soon as it is paid (before any member account exists)."""
    from apps.members.models import MembershipRegistration
    cfg = config()
    reg = MembershipRegistration.objects.select_related('plan').filter(pk=reg_id).first()
    if (reg is None or not is_on(cfg) or reg.payment_status != 'paid' or not reg.payment_amount
            or (cfg.enabled_at and reg.payment_paid_at and reg.payment_paid_at < cfg.enabled_at)
            or ZatcaDocument.objects.filter(registration=reg, doc_type='invoice').exists()):
        return None
    return issue('invoice', signup_items(reg), payment_label=reg.payment_method or 'Card',
                 customer_name=reg.full_name, registration=reg)


def on_signup_refund(reg_id, reason=''):
    original = ZatcaDocument.objects.filter(registration_id=reg_id, doc_type='invoice').first()
    if original is None or not is_on():
        return None
    return issue('credit', [(f'Refund — signup {original.number}', 1, original.total)],
                 payment_label=original.payment_label, customer_name=original.customer_name,
                 registration=original.registration, original=original, reason=reason or 'Signup not approved')


def link_signup_transaction(reg, txn):
    """Approval records the signup payment in the ledger: point its invoice at that entry."""
    ZatcaDocument.objects.filter(registration=reg, doc_type='invoice', transaction__isnull=True).update(transaction=txn)


# ============================================================
# ONBOARDING (go-live wizard and the zatca_onboard command)
# ============================================================
class OnboardingError(Exception):
    pass


def _compliance_cert(device):
    token = crypto.decrypt(device.compliance_token_enc)
    if not token:
        raise OnboardingError('This device has no compliance certificate yet.')
    return signer.Certificate.from_token(token)


def start_device(environment, actor=None):
    """Step 1: a new key and CSR for `environment` (any unfinished device is archived)."""
    cfg = config()
    if environment not in environments.ENVIRONMENTS:
        raise OnboardingError('Unknown environment.')
    problems = validate_seller(cfg, environment)
    if problems:
        raise OnboardingError(' '.join(problems.values()))
    if not crypto.key_configured():
        raise OnboardingError('ZATCA_ENCRYPTION_KEY is not set on the server.')
    ZatcaDevice.objects.filter(status='onboarding').update(status='archived', archived_at=timezone.now())
    count = ZatcaDevice.objects.count() + 1
    key = csr_mod.new_private_key()
    serial = f'1-SmartGym|2-Web|3-{uuid.uuid4()}'
    common_name = f'SmartGym-EGS-{count}'
    pem = csr_mod.build_csr(key, environment=environment, common_name=common_name, serial=serial,
                            organization=cfg.seller_name_en or cfg.seller_name, organization_unit=cfg.branch_name,
                            vat_number=cfg.vat_number, address=cfg.address_line,
                            business_category=cfg.business_category)
    device = ZatcaDevice.objects.create(environment=environment, common_name=common_name, serial=serial,
                                        private_key_enc=crypto.encrypt(csr_mod.private_key_pem(key)), csr=pem,
                                        created_by=actor if getattr(actor, 'is_authenticated', False) else None)
    log('device_created', device=device, actor=actor, environment=environment)
    return device


def request_compliance(device, otp, actor=None):
    """Step 2: the OTP from the Fatoora portal (Sandbox: 123345) → compliance certificate."""
    otp = str(otp or '').strip()
    if not otp.isdigit() or len(otp) != 6:
        raise OnboardingError('The OTP is the 6-digit code from the Fatoora portal.')
    result = client.compliance(device.environment, csr_mod.csr_for_api(device.csr), otp)
    if not result.ok:
        log('compliance_failed', device=device, actor=actor, status=result.status, errors=result.errors[:5])
        raise OnboardingError('ZATCA refused the request: ' + '; '.join(result.errors[:3]))
    device.compliance_token_enc = crypto.encrypt(result.data['binarySecurityToken'])
    device.compliance_secret_enc = crypto.encrypt(result.data['secret'])
    device.compliance_request_id = str(result.data.get('requestID', ''))
    device.save(update_fields=['compliance_token_enc', 'compliance_secret_enc', 'compliance_request_id'])
    log('compliance_certificate', device=device, actor=actor)
    return device


def run_compliance_checks(device, actor=None):
    """Step 3: a sample invoice, credit note and debit note, signed with the compliance certificate."""
    cfg = config()
    token, secret = crypto.decrypt(device.compliance_token_enc), crypto.decrypt(device.compliance_secret_enc)
    if not token:
        raise OnboardingError('Get the compliance certificate first.')
    pih, results = X.FIRST_PIH, {}
    samples = [('invoice', [('Monthly membership', 1, '300.00'), ('Protein shake', 3, '10.00')], ''),
               ('credit', [('Protein shake', 1, '10.00')], 'Item returned'),
               ('debit', [('Locker fee', 1, '5.00')], 'Undercharged locker fee')]
    for icv, (doc_type, items, reason) in enumerate(samples, start=1):
        issued = timezone.localtime().replace(microsecond=0)
        b = vat.split(items, cfg.vat_rate)
        spec = X.Document(doc_type, f'SG{device.id}-TEST-{icv:06d}', str(uuid.uuid4()), issued, icv, pih,
                          cfg.seller(), b, billing_reference=f'SG{device.id}-TEST-000001' if reason else '',
                          reason=reason)
        signed = _sign(device, cfg, spec, b, issued, production=False)
        pih = signed.invoice_hash
        r = client.compliance_check(device.environment, token, secret, signed.invoice_hash, spec.uuid, signed.xml)
        results[doc_type] = {'ok': r.ok and not r.validation.get('errorMessages'),
                             'status': r.data.get('reportingStatus') or f'HTTP {r.status}',
                             'warnings': r.warnings, 'errors': r.errors if not r.ok else []}
    device.compliance_results = results
    device.save(update_fields=['compliance_results'])
    passed = all(v['ok'] for v in results.values())
    log('compliance_checks', device=device, actor=actor, passed=passed,
        results={k: v['status'] for k, v in results.items()})
    if not passed:
        raise OnboardingError('Some compliance checks failed — see the results.')
    return results


def request_production(device, actor=None):
    """Step 4: the production certificate. The device is then ready to activate."""
    if not all(v.get('ok') for v in (device.compliance_results or {}).values()) or len(device.compliance_results or {}) < 3:
        raise OnboardingError('Pass the compliance checks first.')
    result = client.production_csid(device.environment, crypto.decrypt(device.compliance_token_enc),
                                    crypto.decrypt(device.compliance_secret_enc), device.compliance_request_id)
    if not result.ok:
        log('production_failed', device=device, actor=actor, status=result.status, errors=result.errors[:5])
        raise OnboardingError('ZATCA refused the production certificate: ' + '; '.join(result.errors[:3]))
    cert = signer.Certificate.from_token(result.data['binarySecurityToken'])
    device.production_token_enc = crypto.encrypt(result.data['binarySecurityToken'])
    device.production_secret_enc = crypto.encrypt(result.data['secret'])
    device.certificate, device.certificate_issuer = cert.body, cert.issuer_name[:255]
    device.certificate_expires_at = cert.not_after
    device.save(update_fields=['production_token_enc', 'production_secret_enc', 'certificate',
                               'certificate_issuer', 'certificate_expires_at'])
    log('production_certificate', device=device, actor=actor, expires=cert.not_after.isoformat())
    return device


def activate(device, actor=None):
    """Step 5: this device issues every invoice from now on; the previous one is archived."""
    if not device.certificate:
        raise OnboardingError('Get the production certificate first.')
    cfg = config()
    with db_transaction.atomic():
        ZatcaDevice.objects.filter(status='active').exclude(pk=device.pk).update(status='archived',
                                                                                 archived_at=timezone.now())
        device.status, device.activated_at = 'active', timezone.now()
        device.save(update_fields=['status', 'activated_at'])
        cfg.environment = device.environment
        if not cfg.enabled:
            cfg.enabled, cfg.enabled_at = True, timezone.now()
        cfg.save()
    log('device_activated', device=device, actor=actor, environment=device.environment)
    return device


# ============================================================
# SELLER DETAILS
# ============================================================
def locked_fields(cfg=None):
    """Once a legal-or-simulation device is live, the details its certificate carries can't change."""
    cfg = cfg or config()
    device = active_device(cfg)
    if device and device.environment != 'sandbox':
        return ['vat_number', 'crn', 'environment']
    return []


def validate_seller(cfg, environment=None):
    environment = environment or cfg.environment
    e = {}
    if not (cfg.seller_name_ar or cfg.seller_name_en):
        e['seller_name_ar'] = 'Enter the seller name (Arabic or English).'
    v = cfg.vat_number or ''
    if not (len(v) == 15 and v.isdigit() and v[0] == '3' and v[-1] == '3'):
        e['vat_number'] = 'VAT number must be 15 digits and start and end with 3. · الرقم الضريبي 15 رقمًا يبدأ وينتهي بـ 3.'
    elif environment != 'sandbox' and v == environments.SANDBOX_VAT:
        e['vat_number'] = 'Use the business’s real VAT number outside Sandbox.'
    if not (len(cfg.crn or '') == 10 and cfg.crn.isdigit()):
        e['crn'] = 'Commercial registration number must be 10 digits. · رقم السجل التجاري 10 أرقام.'
    if not (len(cfg.building_number or '') == 4 and cfg.building_number.isdigit()):
        e['building_number'] = 'Building number must be 4 digits. · رقم المبنى 4 أرقام.'
    if cfg.additional_number and not (len(cfg.additional_number) == 4 and cfg.additional_number.isdigit()):
        e['additional_number'] = 'Additional number must be 4 digits. · الرقم الإضافي 4 أرقام.'
    if not (len(cfg.postal_code or '') == 5 and cfg.postal_code.isdigit()):
        e['postal_code'] = 'Postal code must be 5 digits. · الرمز البريدي 5 أرقام.'
    for f, label in (('street', 'street'), ('district', 'district'), ('city', 'city')):
        if not (getattr(cfg, f) or '').strip():
            e[f] = f'Enter the {label}.'
    if not (Decimal('0') <= Decimal(cfg.vat_rate) <= Decimal('100')):
        e['vat_rate'] = 'VAT rate must be between 0 and 100.'
    return e
