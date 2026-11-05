"""
Approval + payment logic for MembershipRegistration.
"""
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import is_password_usable
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import get_random_string
from django.conf import settings
from decimal import Decimal
import logging

from apps.core.i18n import bi, language
from apps.core.mail import send_localized

from .models import MembershipRegistration, Membership

User = get_user_model()
logger = logging.getLogger(__name__)


# ============================================================
# PLAN → EXPIRY DATE
# ============================================================
def compute_expiry(start: date, months: int) -> date:
    """
    Add N calendar months to start date. Handles month-length edge cases
    (e.g. Jan 31 + 1 month = Feb 28/29).
    """
    return start + relativedelta(months=months)


# ============================================================
# APPROVE ONE REGISTRATION
# ============================================================
@transaction.atomic
def approve_registration(reg: MembershipRegistration, approved_by=None):
    """
    Approve a single registration:
      1. Create a User account (role='member')
      2. Create a Membership (dates computed from plan duration)
      3. Link both back to the registration
      4. Mark status='approved'
    Returns the created User.

    Raises ValueError if the registration can't be approved.
    """
    if reg.status == MembershipRegistration.STATUS_APPROVED:
        raise ValueError('Registration is already approved.')

    if reg.status == MembershipRegistration.STATUS_REJECTED:
        raise ValueError('Registration was rejected — cannot approve.')

    if reg.created_user_id:
        raise ValueError('A user has already been created for this registration.')

    if reg.payment_status != 'paid':
        raise ValueError('Registration has not been paid — cannot approve.')

    # Live re-check with Moyasar — payment_status alone isn't proof (refunds, manual edits).
    from apps.public_api.moyasar import confirm_paid_for_approval, MoyasarError
    try:
        confirm_paid_for_approval(reg)
    except MoyasarError as exc:
        raise ValueError(str(exc)) from exc

    # 1. Guard against email / iqama collisions (defensive — unique fields should prevent)
    if User.objects.filter(email=reg.email).exists():
        raise ValueError(f'A user with email {reg.email} already exists.')

    # 2. Create the User
    user = User(
        email=reg.email,
        username=reg.email,          # username is required on AbstractUser
        first_name=_split_name(reg.full_name)[0],
        last_name=_split_name(reg.full_name)[1],
        phone=reg.phone,
        language=reg.language,       # emails and notifications in the language they signed up in
        role='member',
        is_verified=True,            # admin approved = verified
        is_active=True,
    )
    # Normally the member logs in with the password chosen at signup.
    # Signups made while the site had no password field have an unusable hash:
    # those get an emailed temporary password and the change-password lock.
    temp_password = None
    if is_password_usable(reg.password_hash):
        user.password = reg.password_hash          # already hashed at signup
    else:
        temp_password = generate_temp_password()
        user.set_password(temp_password)
        user.must_change_password = True
    user.save()

    # 3. Create the Membership
    start = date.today()
    expiry = compute_expiry(start, reg.duration_months)

    membership = Membership.objects.create(
        member=user,
        plan=reg.plan,
        status='active',
        start_date=start,
        expiry_date=expiry,
        iqama_number=reg.iqama_number,
        lifetime_value=reg.payment_amount or reg.total_price,
    )

    # Facility add-ons paid at signup run alongside the membership.
    from .facilities import grant
    for line in reg.facilities or []:
        ends = min(compute_expiry(start, int(line['months'])), expiry)
        grant(user, line['id'], line['name'], Decimal(line['monthly_price']), line['months'],
              Decimal(line['amount']), start, ends, 'signup')

    # 4. The signup payment appears in the member's Payments page.
    record_signup_payment(reg, user)

    # 5. Link back
    reg.created_user = user
    reg.status = MembershipRegistration.STATUS_APPROVED
    reg.approved_at = timezone.now()
    if approved_by is not None:
        reg.approved_by = approved_by
    reg.save(update_fields=[
        'created_user', 'status', 'approved_at', 'approved_by',
    ])

    logger.info(
        'Approved registration #%s → user %s, membership %s, expires %s',
        reg.id, user.email, membership.pass_id, expiry,
    )

    # 6. Welcome email — only once the approval is committed, so a rolled-back
    #    approval never emails login details that don't work.
    transaction.on_commit(lambda: _send_welcome_email(user, membership, temp_password))

    return user


def record_signup_payment(reg, user):
    """Ledger entry (Transaction + paid Invoice) for the Moyasar payment made at signup."""
    from apps.payments.gateway import record_transaction
    from apps.payments.models import Transaction
    ref = reg.moyasar_payment_id or reg.payment_reference
    if not ref or Transaction.objects.filter(member=user, gateway_reference=ref).exists():
        return None
    txn = record_transaction(
        user, f'Membership — {reg.plan.display_name} ({reg.duration_months} mo)'
              + (f' + {len(reg.facilities)} facilit{"y" if len(reg.facilities) == 1 else "ies"}' if reg.facilities else ''),
        'subscription',
        reg.payment_amount or reg.total_price, (reg.payment_method or 'card').upper(), ref,
        when=reg.payment_paid_at or timezone.now(),
        email=False,     # the receipt was emailed when the signup payment went through
        invoice=False,   # ...and its ZATCA invoice was issued then too
    )
    from apps.zatca import service as zatca
    zatca.after_commit(zatca.link_signup_transaction, reg, txn)
    return txn


# ============================================================
# REJECT ONE
# ============================================================
def reject_registration(reg: MembershipRegistration, reason: str = '', rejected_by=None, refund=False):
    """
    Reject a signup. With refund=True a paid signup's Moyasar payment is refunded in full
    first (a failed refund leaves the signup untouched). No ledger entry exists yet — the
    signup payment is only recorded when the registration is approved.
    """
    if reg.status == MembershipRegistration.STATUS_APPROVED:
        raise ValueError('Cannot reject an already-approved registration.')
    if reg.status == MembershipRegistration.STATUS_REJECTED:
        raise ValueError('This registration is already rejected.')

    refunded = False
    if refund and reg.payment_status == 'paid':
        from apps.public_api import moyasar
        try:
            moyasar.refund_payment(reg.moyasar_payment_id, int(reg.payment_amount * 100))
        except moyasar.MoyasarError as exc:
            raise ValueError(f'Refund failed: {exc}') from exc
        reg.payment_status = 'refunded'
        refunded = True
        from apps.zatca import service as zatca
        zatca.after_commit(zatca.on_signup_refund, reg.id, reason)

    reg.status = MembershipRegistration.STATUS_REJECTED
    reg.rejection_reason = reason or 'No reason provided.'
    reg.save(update_fields=['status', 'rejection_reason', 'payment_status'])
    logger.info('Rejected registration #%s (refunded=%s): %s', reg.id, refunded, reason)

    en = [f'Hi {reg.full_name},', '', 'We could not approve your Smart Gym registration.']
    ar = [f'مرحبًا {reg.full_name}،', '', 'لم نتمكن من الموافقة على تسجيلك في سمارت جيم.']
    if reason:
        en.append(f'Reason: {reason}')
        ar.append(f'السبب: {reason}')
    if refunded:
        en += ['', f'Your payment of SAR {reg.payment_amount} has been refunded to your card. '
                   'It can take 3–10 working days to appear.']
        ar += ['', f'تم استرداد مبلغ {reg.payment_amount} ريال إلى بطاقتك، وقد يستغرق ظهوره من 3 إلى 10 أيام عمل.']
    en += ['', 'Questions? Reply to this email or visit the front desk.', '', '— Smart Gym']
    ar += ['', 'لأي استفسار، رُد على هذه الرسالة أو تفضل بزيارة مكتب الاستقبال.', '', '— سمارت جيم']
    try:
        send_localized(reg.email, reg.language, bi('Your Smart Gym registration', 'تسجيلك في سمارت جيم'),
                       bi('\n'.join(en), '\n'.join(ar)))
    except Exception:   # noqa: BLE001 — the rejection stands even if the email fails
        logger.exception('Rejection email for registration #%s failed', reg.id)
    return refunded
# ============================================================
# BULK APPROVE
# ============================================================
def bulk_approve_registrations(queryset, approved_by=None):
    """
    Approve every registration in queryset.
    Returns (approved_count, failed_list) where failed_list is [(reg_id, error_str)].
    """
    approved_count = 0
    failed = []

    for reg in queryset:
        try:
            approve_registration(reg, approved_by=approved_by)
            approved_count += 1
        except Exception as exc:
            logger.exception('Bulk approve failed for reg #%s', reg.id)
            failed.append((reg.id, str(exc)))

    return approved_count, failed


# ============================================================
# HELPERS
# ============================================================
def _split_name(full_name: str):
    parts = (full_name or '').strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else '', ''


# No look-alike characters (0/O, 1/l/I) so it can be typed from an email.
TEMP_PASSWORD_CHARS = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
TEMP_PASSWORD_LENGTH = 12
DEFAULT_MEMBER_PORTAL = 'https://gym-member-and-admin.vercel.app'
CONSOLE_EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


def generate_temp_password():
    """12 chars from a 54-symbol alphabet (~69 bits), via the secrets module.
    (BaseUserManager.make_random_password() was removed in Django 5.1.)"""
    return get_random_string(TEMP_PASSWORD_LENGTH, TEMP_PASSWORD_CHARS)


def member_portal_url():
    """
    One URL for emails. FRONTEND_URL is a comma-separated origin list in
    production ("http://localhost:5173,https://gym-member-and-admin.vercel.app"),
    so prefer its first https entry; locally that is the localhost one.
    """
    urls = [u.strip().rstrip('/') for u in settings.FRONTEND_URL.split(',') if u.strip()]
    https = [u for u in urls if u.startswith('https://')]
    return (https or urls or [DEFAULT_MEMBER_PORTAL])[0]


def _send_welcome_email(user, membership, temp_password=None):
    """
    Emails the approval + login details. Normally the member logs in with the
    password they chose at signup. temp_password is only set for signups made
    while the site had no password field (see approve_registration); it is
    never logged, and in production the console backend is refused because it
    would print the email (member details, maybe a password) into the logs.
    """
    if settings.EMAIL_BACKEND == CONSOLE_EMAIL_BACKEND and not settings.DEBUG:
        logger.error(
            'Welcome email for %s NOT sent: EMAIL_BACKEND is the console backend, which would '
            'print it into the server logs. Configure SMTP.', user.email,
        )
        return

    if temp_password:
        login = (
            f"YOUR LOGIN\n"
            f"  Login page:         {member_portal_url()}/login\n"
            f"  Email:              {user.email}\n"
            f"  Temporary password: {temp_password}\n\n"
            f"IMPORTANT: this is a temporary password. You will be asked to choose your own\n"
            f"password when you first log in. Never share it - Smart Gym staff will never ask for it.\n\n"
        )
    else:
        login = (
            f"You can now log in with the email and password you created during signup.\n\n"
            f"  Login page:  {member_portal_url()}/login\n"
            f"  Email:       {user.email}\n\n"
        )

    subject = bi('Welcome to Smart Gym — your membership is active', 'مرحبًا بك في سمارت جيم — عضويتك مفعّلة')
    plan_ar = membership.plan.display_name_ar or membership.plan.display_name
    login_ar = (
        f"  كلمة المرور المؤقتة: {temp_password}\n\nمهم: هذه كلمة مرور مؤقتة، وسيُطلب منك اختيار كلمة مرورك عند أول "
        f"تسجيل دخول. لا تشاركها مع أحد — موظفو سمارت جيم لن يطلبوها منك أبدًا.\n\n"
        if temp_password else "يمكنك الآن تسجيل الدخول بالبريد الإلكتروني وكلمة المرور التي أنشأتها عند التسجيل.\n"
    )
    body_en = (
        f"Hi {user.first_name or user.email},\n\n"
        f"Your registration is approved! Your Smart Gym membership is now active.\n\n"
        f"  Pass ID:     {membership.pass_id}\n"
        f"  Iqama #:     {membership.iqama_number}\n"
        f"  Plan:        {membership.plan.display_name}\n"
        f"  Valid until: {membership.expiry_date}\n\n"
        f"{login}"
        f"See you on the floor.\n— Smart Gym"
    )
    body_ar = (
        f"مرحبًا {user.first_name or user.email}،\n\n"
        f"تمت الموافقة على تسجيلك، وعضويتك في سمارت جيم مفعّلة الآن.\n\n"
        f"  رقم البطاقة: {membership.pass_id}\n"
        f"  رقم الإقامة: {membership.iqama_number}\n"
        f"  الخطة: {plan_ar}\n"
        f"  صالحة حتى: {membership.expiry_date}\n\n"
        f"{login_ar}"
        f"  صفحة الدخول: {member_portal_url()}/login\n"
        f"  البريد الإلكتروني: {user.email}\n\n"
        f"نراك في النادي.\n— سمارت جيم"
    )
    try:
        send_localized(user.email, language(user), subject, bi(body_en, body_ar))
    except Exception:
        # Approval is already committed; don't fail it — but make it visible (without the body).
        logger.exception('Welcome email to %s failed to send.', user.email)
        return
    logger.info('Welcome email sent to %s (membership %s).', user.email, membership.pass_id)