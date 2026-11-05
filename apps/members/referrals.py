"""
Member referrals.

Every member has a referral code (made the first time they look at it) and a link to the website's
signup page with it: <PUBLIC_SITE_URL>/join?ref=CODE. The website sends the code with the signup
(`referral_code`). When the gym approves that signup — so it is paid and checked by staff, not just
typed in — the referrer gets REFERRAL_REWARD_SAR (default SAR 50) of locker credit and a notification.

The reward is paid at most once per new member, never to someone referring themselves (same Iqama or
phone), and only to an active member account. It shows in the referrer's payments as a "Locker credit"
line, which keeps it out of revenue (no money moved).
"""
import logging
import secrets
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Sum

from apps.core.i18n import bi

logger = logging.getLogger(__name__)
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # no 0/O or 1/I
CODE_LENGTH = 8


def reward_amount():
    return Decimal(str(getattr(settings, 'REFERRAL_REWARD_SAR', '50'))).quantize(Decimal('0.01'))


def normalize(code):
    return ''.join(ch for ch in str(code or '').upper() if ch.isalnum())[:12]


def code_for(user):
    """The member's referral code, made on first use."""
    if user.referral_code:
        return user.referral_code
    from apps.accounts.models import User
    for _ in range(10):
        code = ''.join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        try:
            with transaction.atomic():
                if User.objects.filter(pk=user.pk, referral_code__isnull=True).update(referral_code=code):
                    user.referral_code = code
                    return code
            user.refresh_from_db(fields=['referral_code'])
            return user.referral_code
        except IntegrityError:          # another member already has it: try another
            continue
    raise RuntimeError('Could not make a unique referral code.')


def link_for(user):
    base = (getattr(settings, 'PUBLIC_SITE_URL', '') or '').rstrip('/')
    return f'{base}/join?ref={code_for(user)}'


def referrer_for(code):
    """The active member a code belongs to, or None."""
    code = normalize(code)
    if not code:
        return None
    from apps.accounts.models import User
    return User.objects.filter(referral_code=code, role='member', is_active=True).first()


def summary(user):
    from .models import Referral
    rows = Referral.objects.filter(referrer=user)
    return {
        'code': code_for(user), 'link': link_for(user), 'reward': str(reward_amount()),
        'joined': rows.count(),
        'earned': str(Decimal(rows.aggregate(t=Sum('reward'))['t'] or 0).quantize(Decimal('0.01'))),
    }


def reward_on_approval(reg, new_member):
    """Credit the referrer of an approved signup (no-op without a valid code). Returns the Referral or None."""
    from apps.payments.gateway import record_transaction
    from apps.payments.models import LockerCredit
    from apps.notifications.service import notify
    from .models import Membership, Referral

    referrer = referrer_for(reg.referral_code)
    if referrer is None or referrer.pk == new_member.pk:
        return None
    same_person = (Membership.objects.filter(member=referrer, iqama_number=reg.iqama_number).exists()
                   or (reg.phone and referrer.phone == reg.phone))
    if same_person:
        logger.info('Referral %s on registration #%s skipped: same person', reg.referral_code, reg.id)
        return None
    amount = reward_amount()
    with transaction.atomic():
        referral, created = Referral.objects.get_or_create(
            referred=new_member, defaults={'referrer': referrer, 'registration': reg, 'reward': amount})
        if not created or amount <= 0:
            return referral if created else None
        credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=referrer)
        LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') + amount)
        record_transaction(referrer, f'Referral reward — {new_member.first_name or "a new member"} joined', 'topup',
                           amount, 'Locker credit', f'referral:{referral.id}', email=False, invoice=False)
    name = new_member.first_name or new_member.email
    transaction.on_commit(lambda: notify(
        referrer, 'system', bi('Referral reward', 'مكافأة الإحالة'),
        bi(f'{name} joined with your link — SAR {amount} is in your locker credit.',
           f'انضم {name} عبر رابطك — أُضيف {amount} ريال إلى رصيد خزانتك.'), icon='redeem'))
    return referral
