"""
Two-step sign-in. After the password is right, the person proves it's them with a 6-digit code
from an authenticator app (TOTP, RFC 6238 — Google/Microsoft Authenticator, 1Password…) or a
code sent to their email. Each person picks the method when they turn it on.

Required for everyone who signs in to a staff portal (REQUIRED_ROLES); members can turn it on
from their profile. The browser only ever holds a short-lived ticket until the second step
passes — no tokens are issued before that.

The code is asked for once per device: after it passes, the browser can be trusted for
TRUST_DAYS days (TrustedDevice). Admins can switch 2-step verification off for the whole gym, or
make it optional for staff (Admin → Settings → Security; SystemSettings.security).
"""
import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import TrustedDevice, TwoFactor, TwoFactorChallenge

logger = logging.getLogger(__name__)

REQUIRED_ROLES = {'admin', 'manager', 'trainer'}
METHODS = ('totp', 'email')
ISSUER = 'Smart Gym'
STEP_SECONDS = 30
TICKET_MINUTES = 10
MAX_ATTEMPTS = 5          # wrong codes per ticket
MAX_SENDS = 3             # emailed codes per ticket
RESEND_GAP_SECONDS = 30
BACKUP_CODES = 8
TRUST_DAYS = 30


class TwoFactorError(Exception):
    """Shown to the person (wrong code, expired ticket…). `field` says where."""

    def __init__(self, message, field='code', status=400):
        super().__init__(message)
        self.field = field
        self.status = status


def policy():
    """{'enabled': ask for codes at all, 'required_for_staff': staff roles must set it up}."""
    from apps.admin_api.models import SystemSettings
    sec = SystemSettings.get_solo().security or {}
    return {'enabled': sec.get('two_factor_enabled', True) is not False,
            'required_for_staff': sec.get('two_factor_required_for_staff', True) is not False}


def is_required(user):
    p = policy()
    return p['enabled'] and p['required_for_staff'] and user.role in REQUIRED_ROLES


def settings_for(user):
    return TwoFactor.objects.filter(user=user, enabled=True).first()


# ============================================================
# TRUSTED DEVICES ("don't ask again on this device for 30 days")
# ============================================================
def trust_device(user, label=''):
    """A new device token for the browser to keep (shown once; only its hash is stored)."""
    token = secrets.token_urlsafe(32)
    TrustedDevice.objects.create(user=user, token_hash=_hash(token), label=label[:120],
                                 expires_at=timezone.now() + timedelta(days=TRUST_DAYS))
    TrustedDevice.objects.filter(user=user, expires_at__lt=timezone.now()).delete()
    return token


def is_trusted(user, token):
    if not token or not isinstance(token, str) or len(token) > 100:
        return False
    d = TrustedDevice.objects.filter(user=user, token_hash=_hash(token), expires_at__gt=timezone.now()).first()
    if d is None:
        return False
    TrustedDevice.objects.filter(id=d.id).update(last_used_at=timezone.now())
    return True


def forget_devices(user):
    """Every trusted browser asks for a code again (password or 2-step settings changed)."""
    TrustedDevice.objects.filter(user=user).delete()


def device_label(request):
    ua = request.META.get('HTTP_USER_AGENT', '')
    browser = next((b for b in ('Edg', 'OPR', 'Chrome', 'Firefox', 'Safari') if b in ua), 'Browser')
    system = next((o for o in ('Windows', 'Android', 'iPhone', 'iPad', 'Mac OS', 'Linux') if o in ua), '')
    return f"{ {'Edg': 'Edge', 'OPR': 'Opera'}.get(browser, browser)}{' on ' + system if system else ''}"


# ============================================================
# CODES
# ============================================================
def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def _totp(secret, step):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack('>Q', step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f'{(struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}'


def totp_now(secret, at=None):
    return _totp(secret, int((at or time.time()) // STEP_SECONDS))


def match_totp(secret, code, last_step=0, at=None):
    """The time step the code belongs to (±1 step for clock drift), or None. Old steps never work again."""
    now = int((at or time.time()) // STEP_SECONDS)
    for step in (now, now - 1, now + 1):
        if step > last_step and hmac.compare_digest(_totp(secret, step), code):
            return step
    return None


def otpauth_uri(user, secret):
    label = quote(f'{ISSUER}:{user.email}')
    return f'otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits=6&period={STEP_SECONDS}'


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _clean(code):
    return ''.join(str(code or '').split()).replace('-', '').lower()


def new_backup_codes():
    """(shown once to the person, stored hashes)."""
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    codes = [''.join(secrets.choice(alphabet) for _ in range(8)) for _ in range(BACKUP_CODES)]
    shown = [f'{c[:4]}-{c[4:]}' for c in codes]
    return shown, [_hash(c) for c in codes]


def email_hint(email):
    name, _, domain = email.partition('@')
    return f'{name[:1]}{"•" * max(len(name) - 1, 2)}@{domain}'


def email_available():
    from apps.members.services import CONSOLE_EMAIL_BACKEND
    return not (settings.EMAIL_BACKEND == CONSOLE_EMAIL_BACKEND and not settings.DEBUG)


def _send_email_code(user, code, purpose):
    if not email_available():
        raise TwoFactorError('Email isn’t set up on the server, so codes can’t be emailed. Use an authenticator app.',
                             field='method')
    what = 'sign in' if purpose == 'login' else 'turn on 2-step verification'
    what_ar = 'تسجيل الدخول' if purpose == 'login' else 'تفعيل التحقق بخطوتين'
    try:
        send_mail(
            f'Smart Gym code: {code} · رمز سمارت جيم',
            f"Hi {user.first_name or user.email},\n\nYour Smart Gym code to {what} is:\n\n    {code}\n\n"
            f"It works once and expires in {TICKET_MINUTES} minutes. If you didn't try to sign in, change your "
            "password — someone knows it.\n— Smart Gym\n\n────────────\n\n"
            f"رمز {what_ar} في سمارت جيم هو:\n\n    {code}\n\nيعمل مرة واحدة وينتهي خلال {TICKET_MINUTES} دقائق.\n— سمارت جيم",
            settings.DEFAULT_FROM_EMAIL, [user.email],
        )
    except Exception:   # noqa: BLE001
        logger.exception('2FA code email to %s failed.', user.email)
        raise TwoFactorError('We couldn’t email the code right now. Try again in a minute or use an authenticator app.',
                             field='method', status=503)


# ============================================================
# CHALLENGES (tickets)
# ============================================================
def _new_challenge(user, purpose, method='', pending_secret=''):
    ticket = secrets.token_urlsafe(32)
    TwoFactorChallenge.objects.filter(user=user, purpose=purpose, used=False).update(used=True)   # one open at a time
    ch = TwoFactorChallenge.objects.create(
        user=user, purpose=purpose, ticket_hash=_hash(ticket), method=method, pending_secret=pending_secret,
        expires_at=timezone.now() + timedelta(minutes=TICKET_MINUTES),
    )
    if TwoFactorChallenge.objects.count() % 50 == 0:   # cheap occasional prune
        TwoFactorChallenge.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
    return ticket, ch


def _challenge(ticket, purposes):
    ch = (TwoFactorChallenge.objects.select_for_update().select_related('user')
          .filter(ticket_hash=_hash(str(ticket or '')), purpose__in=purposes).first())
    if ch is None or ch.used or ch.expires_at < timezone.now() or not ch.user.is_active:
        raise TwoFactorError('This sign-in step expired. Sign in again.', field='ticket', status=401)
    if ch.attempts >= MAX_ATTEMPTS:
        raise TwoFactorError('Too many wrong codes. Sign in again.', field='ticket', status=401)
    return ch


def _email_code(ch):
    if ch.sends >= MAX_SENDS:
        raise TwoFactorError('Too many codes sent. Sign in again to get a new one.', field='ticket', status=429)
    if ch.sends and (timezone.now() - ch.created_at).total_seconds() < RESEND_GAP_SECONDS * ch.sends:
        raise TwoFactorError('Wait a few seconds before asking for another code.', status=429)
    code = f'{secrets.randbelow(1_000_000):06d}'
    _send_email_code(ch.user, code, ch.purpose)
    ch.code_hash = _hash(f'{ch.ticket_hash}:{code}')
    ch.sends += 1
    ch.save(update_fields=['code_hash', 'sends'])


def _wrong(ch, message='That code isn’t right. Check it and try again.'):
    """Counts a wrong code and returns the error to raise — after the transaction commits, so the count sticks."""
    ch.attempts += 1
    ch.save(update_fields=['attempts'])
    left = MAX_ATTEMPTS - ch.attempts
    if left <= 0:
        return TwoFactorError('Too many wrong codes. Sign in again.', field='ticket', status=401)
    return TwoFactorError(f'{message} {left} {"try" if left == 1 else "tries"} left.' if left <= 2 else message)


def _check_code(ch, tf, code, allow_backup):
    """True when `code` passes for this challenge (TOTP / emailed / backup). Updates replay state."""
    raw = _clean(code)
    method = ch.method or (tf.method if tf else '')
    if method == 'email':
        if raw.isdigit() and len(raw) == 6 and ch.code_hash and hmac.compare_digest(
                ch.code_hash, _hash(f'{ch.ticket_hash}:{raw}')):
            return True
    elif method == 'totp':
        secret = ch.pending_secret or (tf.secret if tf else '')
        if raw.isdigit() and len(raw) == 6 and secret:
            step = match_totp(secret, raw, last_step=tf.last_totp_step if tf and not ch.pending_secret else 0)
            if step is not None:
                if tf and not ch.pending_secret:
                    tf.last_totp_step = step
                    tf.save(update_fields=['last_totp_step'])
                return True
    if allow_backup and tf and len(raw) == 8 and _hash(raw) in (tf.backup_codes or []):
        tf.backup_codes = [h for h in tf.backup_codes if h != _hash(raw)]
        tf.save(update_fields=['backup_codes'])
        logger.warning('Backup code used by %s (%d left).', ch.user.email, len(tf.backup_codes))
        return True
    return False


# ============================================================
# SIGN-IN
# ============================================================
def start_login(user, device_token=None):
    """
    None when no second step is needed; otherwise the response body for the browser:
    {two_factor: 'verify', method, ticket, email_hint}  — 2FA is on: enter the code
    {two_factor: 'setup', ticket, methods, email_hint}  — the role needs 2FA and it's off: set it up
    No step when the gym switched 2-step verification off, or this device is trusted.
    """
    if not policy()['enabled']:
        return None
    tf = settings_for(user)
    if tf and is_trusted(user, device_token):
        return None
    if tf:
        ticket, ch = _new_challenge(user, 'login', method=tf.method)
        body = {'two_factor': 'verify', 'method': tf.method, 'ticket': ticket, 'email_hint': email_hint(user.email),
                'backup_codes_left': len(tf.backup_codes or [])}
        if tf.method == 'email':
            with transaction.atomic():
                _email_code(TwoFactorChallenge.objects.select_for_update().get(id=ch.id))
            body['detail'] = f'We emailed a 6-digit code to {email_hint(user.email)}.'
        else:
            body['detail'] = 'Enter the 6-digit code from your authenticator app.'
        return body
    if is_required(user):
        ticket, _ = _new_challenge(user, 'setup')
        return {'two_factor': 'setup', 'ticket': ticket, 'methods': list(METHODS),
                'email_available': email_available(), 'email_hint': email_hint(user.email),
                'detail': 'Your account needs 2-step verification. Set it up once to continue.'}
    return None


def verify_login(ticket, code):
    """The signed-in user once the code passes. Raises TwoFactorError."""
    with transaction.atomic():
        ch = _challenge(ticket, ['login'])
        tf = settings_for(ch.user)
        if tf is None:
            raise TwoFactorError('2-step verification was turned off. Sign in again.', field='ticket', status=401)
        if not _check_code(ch, tf, code, allow_backup=True):
            error = _wrong(ch)
        else:
            error = None
            ch.used = True
            ch.save(update_fields=['used'])
    if error:
        raise error
    return ch.user


def resend(ticket):
    with transaction.atomic():
        ch = _challenge(ticket, ['login', 'setup', 'enable'])
        if (ch.method or '') != 'email':
            raise TwoFactorError('This step uses an authenticator app — open the app for the code.', field='method')
        _email_code(ch)
    return {'detail': f'A new code is on its way to {email_hint(ch.user.email)}.'}


# ============================================================
# TURNING IT ON (at sign-in when required, or from the profile)
# ============================================================
def choose_method(ticket, method, purposes=('setup', 'enable'), user=None):
    """Step 1 of setup: totp → a key + QR link to add to the app; email → a code is emailed."""
    if method not in METHODS:
        raise TwoFactorError('Choose an authenticator app or email.', field='method')
    with transaction.atomic():
        ch = _challenge(ticket, list(purposes))
        if user is not None and ch.user_id != user.id:
            raise TwoFactorError('This step belongs to another account.', field='ticket', status=401)
        ch.method = method
        ch.pending_secret = new_secret() if method == 'totp' else ''
        ch.code_hash = ''
        ch.save(update_fields=['method', 'pending_secret', 'code_hash'])
        if method == 'email':
            _email_code(ch)
            return {'method': 'email', 'email_hint': email_hint(ch.user.email),
                    'detail': f'We emailed a 6-digit code to {email_hint(ch.user.email)}.'}
        return {'method': 'totp', 'secret': ch.pending_secret, 'otpauth_uri': otpauth_uri(ch.user, ch.pending_secret),
                'detail': 'Scan the QR code with your authenticator app, then enter the 6-digit code it shows.'}


def confirm_method(ticket, code, purposes=('setup', 'enable'), user=None):
    """Step 2: the first code proves the method works → 2FA is on. Returns (user, backup codes to show)."""
    with transaction.atomic():
        ch = _challenge(ticket, list(purposes))
        if user is not None and ch.user_id != user.id:
            raise TwoFactorError('This step belongs to another account.', field='ticket', status=401)
        if ch.method not in METHODS:
            raise TwoFactorError('Choose a method first.', field='method')
        if not _check_code(ch, None, code, allow_backup=False):
            error = _wrong(ch)
        else:
            error = None
            shown, hashes = new_backup_codes()
            TwoFactor.objects.update_or_create(user=ch.user, defaults={
                'method': ch.method, 'secret': ch.pending_secret if ch.method == 'totp' else '', 'enabled': True,
                'backup_codes': hashes, 'last_totp_step': 0, 'confirmed_at': timezone.now(),
            })
            forget_devices(ch.user)
            ch.used = True
            ch.save(update_fields=['used'])
    if error:
        raise error
    logger.info('2FA (%s) turned on for %s', ch.method, ch.user.email)
    return ch.user, shown


def start_enable(user):
    """From the profile: a ticket for choose_method / confirm_method."""
    ticket, _ = _new_challenge(user, 'enable')
    return ticket


def check_current(user, code):
    """For changes while signed in (turn off, new backup codes): the current code or a backup code."""
    tf = settings_for(user)
    if tf is None:
        return True
    raw = _clean(code)
    with transaction.atomic():
        tf = TwoFactor.objects.select_for_update().get(id=tf.id)
        if tf.method == 'email' and raw.isdigit() and len(raw) == 6:
            # the code request_current_code() emailed (kept on a closed challenge, never on a ticket)
            prior = (TwoFactorChallenge.objects.select_for_update()
                     .filter(user=user, purpose='enable', method='email', code_hash__gt='',
                             expires_at__gte=timezone.now()).order_by('-created_at').first())
            if prior and hmac.compare_digest(prior.code_hash, _hash(f'{prior.ticket_hash}:{raw}')):
                prior.code_hash = ''
                prior.save(update_fields=['code_hash'])
                return True
            return False
        probe = TwoFactorChallenge(user=user, method=tf.method)      # not saved: only says which method
        return _check_code(probe, tf, raw, allow_backup=True)


def request_current_code(user):
    """Emails a code for check_current() when the person's method is email."""
    tf = settings_for(user)
    if tf is None or tf.method != 'email':
        raise TwoFactorError('Use the code from your authenticator app.', field='method')
    with transaction.atomic():
        _, ch = _new_challenge(user, 'enable', method='email')
        ch = TwoFactorChallenge.objects.select_for_update().get(id=ch.id)
        _email_code(ch)
        ch.used = True          # not a ticket the browser holds — check_current() looks it up
        ch.save(update_fields=['used'])
    return {'detail': f'We emailed a 6-digit code to {email_hint(user.email)}.'}
