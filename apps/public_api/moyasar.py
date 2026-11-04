"""
Moyasar payment integration (server side).

The browser creates the payment with the publishable key through the Moyasar
form, so card data never touches this server. Nothing the browser or the
webhook body claims is trusted: every state change goes through
sync_payment(), which re-fetches the payment from Moyasar with the secret key
and checks it against what we expect for the registration.
"""
import base64
import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from apps.members.models import MembershipRegistration

logger = logging.getLogger(__name__)

CURRENCY = 'SAR'
# Moyasar's API sits behind Cloudflare, which rejects Python's default
# "Python-urllib/x.y" User-Agent with 403 (Cloudflare error 1010).
USER_AGENT = 'SmartGym-Backend/1.0'
PAID_STATUSES = {'paid'}  # guide §6.3: provider status must be exactly "paid"

# Only these provider fields are kept in payment_raw (guide §3: redact the rest).
RAW_FIELDS = ('id', 'status', 'amount', 'fee', 'currency', 'refunded', 'description',
              'metadata', 'created_at', 'updated_at')
RAW_SOURCE_FIELDS = ('type', 'company', 'message', 'reference_number')


class MoyasarError(Exception):
    """The payment can't be used for this registration (client-side problem)."""


class MoyasarUnavailable(MoyasarError):
    """Moyasar couldn't be reached — worth retrying."""


# ============================================================
# CONFIG
# ============================================================
def _secret_key():
    if not settings.MOYASAR_SECRET_KEY:
        raise ImproperlyConfigured('MOYASAR_SECRET_KEY is not set.')
    return settings.MOYASAR_SECRET_KEY


def webhook_token_valid(token):
    """Constant-time check of the secret_token Moyasar sends with each webhook."""
    expected = settings.MOYASAR_WEBHOOK_SECRET
    if not expected:
        raise ImproperlyConfigured('MOYASAR_WEBHOOK_SECRET is not set.')
    return isinstance(token, str) and hmac.compare_digest(token.encode(), expected.encode())


# ============================================================
# API
# ============================================================
def fetch_payment(payment_id):
    """GET /payments/<id> using the secret key (HTTP Basic, key as username)."""
    if not isinstance(payment_id, str) or not payment_id or len(payment_id) > 64:
        raise MoyasarError('Invalid payment id.')

    auth = base64.b64encode(f'{_secret_key()}:'.encode()).decode()
    req = urllib.request.Request(
        f"{settings.MOYASAR_API_URL}/payments/{urllib.parse.quote(payment_id, safe='')}",
        headers={
            'Authorization': f'Basic {auth}',
            'Accept': 'application/json',
            'User-Agent': USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MoyasarError('Payment not found.') from exc
        body = exc.read()[:200]
        if exc.code in (401, 403):
            # Config problem, not an outage — check MOYASAR_SECRET_KEY (and test vs live mode).
            logger.error('Moyasar rejected the request for payment %s: HTTP %s %s', payment_id, exc.code, body)
        else:
            logger.error('Moyasar GET payment %s failed: HTTP %s %s', payment_id, exc.code, body)
        raise MoyasarUnavailable('Could not verify the payment with the provider.') from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.error('Moyasar GET payment %s failed: %s', payment_id, exc)
        raise MoyasarUnavailable('Could not verify the payment with the provider.') from exc


def refund_payment(payment_id, amount_halalas):
    """POST /payments/<id>/refund (secret key). Returns Moyasar's payment; raises MoyasarError."""
    if not isinstance(payment_id, str) or not payment_id:
        raise MoyasarError('No Moyasar payment to refund.')
    auth = base64.b64encode(f'{_secret_key()}:'.encode()).decode()
    req = urllib.request.Request(
        f"{settings.MOYASAR_API_URL}/payments/{urllib.parse.quote(payment_id, safe='')}/refund",
        data=urllib.parse.urlencode({'amount': int(amount_halalas)}).encode(),
        method='POST',
        headers={'Authorization': f'Basic {auth}', 'Accept': 'application/json', 'User-Agent': USER_AGENT,
                 'Content-Type': 'application/x-www-form-urlencoded'},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300]
        logger.error('Moyasar refund %s failed: HTTP %s %s', payment_id, exc.code, body)
        try:
            msg = json.loads(body).get('message')
        except ValueError:
            msg = None
        raise MoyasarError(f'Moyasar refused the refund{": " + msg if msg else ""}.') from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.error('Moyasar refund %s failed: %s', payment_id, exc)
        raise MoyasarUnavailable('Could not reach Moyasar to refund.') from exc


def checkout_params(reg):
    """Everything the frontend needs to initialise the Moyasar form for a registration."""
    return {
        'publishable_key': settings.MOYASAR_PUBLISHABLE_KEY,
        'amount': reg.amount_halalas,
        'currency': CURRENCY,
        'description': f'Smart Gym — {reg.plan.display_name} ({reg.duration_months} mo) — Reg #{reg.id}',
        'metadata': {'registration_id': str(reg.id), 'attempt': reg.payment_attempt_token},
    }


def _redact(payment):
    raw = {k: payment.get(k) for k in RAW_FIELDS if k in payment}
    source = payment.get('source') or {}
    raw['source'] = {k: source.get(k) for k in RAW_SOURCE_FIELDS if k in source}
    return raw


# ============================================================
# SYNC (callback + webhook + attach all land here)
# ============================================================
def sync_payment(payment_id, registration_id=None, payment=None):
    """
    Re-fetch a payment from Moyasar and apply it to the registration named in
    its metadata. Idempotent, so the callback page and the webhook can both
    call it in either order. If registration_id is given, the payment must
    belong to it. Returns the registration.
    """
    payment = payment or fetch_payment(payment_id)

    metadata = payment.get('metadata') or {}
    meta_reg_id = str(metadata.get('registration_id', ''))
    if not meta_reg_id.isdigit():
        raise MoyasarError('Payment is not linked to a registration.')
    if registration_id is not None and meta_reg_id != str(registration_id):
        raise MoyasarError('Payment does not belong to this registration.')

    with transaction.atomic():
        try:
            reg = MembershipRegistration.objects.select_for_update().get(id=int(meta_reg_id))
        except MembershipRegistration.DoesNotExist:
            raise MoyasarError('Registration not found.')

        attempt = metadata.get('attempt')
        if not (
            reg.payment_attempt_token and isinstance(attempt, str)
            and hmac.compare_digest(attempt.encode(), reg.payment_attempt_token.encode())
        ):
            if payment.get('status') in PAID_STATUSES:
                logger.error(
                    'Paid Moyasar payment %s does not match the current attempt of registration #%s '
                    '— needs manual review/refund.', payment.get('id'), reg.id,
                )
            raise MoyasarError("Payment does not match this registration's payment attempt.")

        _apply_payment(reg, payment)
    return reg


def _apply_payment(reg, payment):
    pid = payment.get('id')
    status = payment.get('status')
    source = payment.get('source') or {}

    if reg.payment_status == 'paid':
        if pid != reg.moyasar_payment_id and status in PAID_STATUSES:
            logger.warning(
                'Registration #%s already paid by %s — extra paid payment %s needs a manual refund.',
                reg.id, reg.moyasar_payment_id, pid,
            )
        return

    # A late failure for a payment we've since replaced (user retried) is stale.
    if status not in PAID_STATUSES and reg.moyasar_payment_id and pid != reg.moyasar_payment_id:
        return

    if MembershipRegistration.objects.filter(moyasar_payment_id=pid).exclude(id=reg.id).exists():
        raise MoyasarError('Payment already used by another registration.')

    reg.moyasar_payment_id = pid
    reg.payment_reference = pid
    reg.payment_raw = _redact(payment)

    if status in PAID_STATUSES:
        problems = []
        if payment.get('amount') != reg.amount_halalas:
            problems.append(f"amount {payment.get('amount')} != expected {reg.amount_halalas} halalas")
        if payment.get('currency') != CURRENCY:
            problems.append(f"currency {payment.get('currency')} != {CURRENCY}")

        if problems:
            # Money was taken but doesn't match — never auto-approve; leave for manual review.
            reg.payment_failure_reason = ('Needs manual review: ' + '; '.join(problems))[:255]
            logger.error('Registration #%s payment %s mismatch: %s', reg.id, pid, problems)
        else:
            reg.payment_status = 'paid'
            reg.payment_paid_at = timezone.now()
            reg.payment_failure_reason = ''
            reg.payment_method = _method_from_source(source)
            if reg.status == MembershipRegistration.STATUS_PENDING:
                reg.status = MembershipRegistration.STATUS_PAID
            else:
                logger.warning(
                    'Registration #%s paid by %s while %s — needs manual review/refund.',
                    reg.id, pid, reg.status,
                )
    elif status == 'failed':
        reg.payment_status = 'failed'
        reg.payment_failure_reason = (source.get('message') or 'Payment failed.')[:255]
    # 'initiated' / 'authorized': still in progress (e.g. 3-D Secure) — leave as pending.

    reg.save(update_fields=[
        'moyasar_payment_id', 'payment_reference', 'payment_raw',
        'payment_status', 'payment_paid_at', 'payment_failure_reason',
        'payment_method', 'status',
    ])
    logger.info('Registration #%s ← Moyasar %s (%s) → payment_status=%s', reg.id, pid, status, reg.payment_status)
    if reg.payment_status == 'paid':   # first time it's paid (an already-paid signup returned earlier)
        from apps.payments.receipts import email_after_commit, from_registration
        from apps.zatca import service as zatca
        zatca.after_commit(zatca.on_signup_paid, reg.id)   # before the email, so the receipt carries it
        email_after_commit(from_registration, reg)


# ============================================================
# APPROVAL GATE
# ============================================================
def confirm_paid_for_approval(reg):
    """
    Re-check the payment with Moyasar right before an admin approves, so a
    refund/void after payment, or a payment_status edited by hand, can't slip
    through. Raises MoyasarError describing every problem found.
    """
    if not reg.moyasar_payment_id:
        raise MoyasarError('No Moyasar payment is recorded for this registration.')

    payment = fetch_payment(reg.moyasar_payment_id)
    problems = []
    if payment.get('status') not in PAID_STATUSES:
        problems.append(f"Moyasar status is '{payment.get('status')}'")
    if payment.get('amount') != reg.amount_halalas:
        problems.append(f"amount {payment.get('amount')} != expected {reg.amount_halalas} halalas")
    if payment.get('currency') != CURRENCY:
        problems.append(f"currency {payment.get('currency')} != {CURRENCY}")
    if payment.get('refunded'):
        problems.append(f"{payment.get('refunded')} halalas refunded")
    attempt = (payment.get('metadata') or {}).get('attempt')
    if not (
        reg.payment_attempt_token and isinstance(attempt, str)
        and hmac.compare_digest(attempt.encode(), reg.payment_attempt_token.encode())
    ):
        problems.append('payment does not match this registration')
    if problems:
        raise MoyasarError('Moyasar verification failed: ' + '; '.join(problems) + '.')


def _method_from_source(source):
    if source.get('type') == 'creditcard':
        return (source.get('company') or 'creditcard')[:20]  # mada / visa / master
    return (source.get('type') or 'unknown')[:20]           # stcpay / applepay
