"""
Locker credit on/off switch: settings.LOCKER_CREDIT_ENABLED (env, off for now).

Off:
  * no top-ups and no paying with credit ("Locker credit is coming soon.");
  * a cancelled booking's price goes back to the card that paid it (Moyasar refund) — or to credit if it was
    paid with credit before the switch — and a payment taken at the desk is refunded by the front desk;
  * balances, history and referral rewards are kept as they are, ready for when it is turned on.
On: everything works as before (cancellations refund to locker credit).
"""
from django.conf import settings
from django.db.models import F

from apps.core.i18n import bi

COMING_SOON = 'Locker credit is coming soon.'


def enabled():
    return bool(getattr(settings, 'LOCKER_CREDIT_ENABLED', False))


class RefundError(Exception):
    pass


def refund_cancellation(person, amount, description, ref, refund_of=None, **intent_filters):
    """
    Give back a cancelled booking's price; call inside the cancellation's transaction.
    `intent_filters` find the paid PaymentIntent (e.g. trainer_session=s). Returns where the money went:
    'credit', 'card' or 'desk' (None for nothing to refund). Raises RefundError when the card refund fails.
    """
    from apps.public_api.moyasar import MoyasarError
    from .gateway import PaymentError, record_transaction, refund_intent
    from .models import LockerCredit, PaymentIntent
    if not amount or amount <= 0:
        return None
    intent = (PaymentIntent.objects.filter(status='paid', **intent_filters).order_by('-paid_at').first()
              if intent_filters else None)
    if enabled() or (intent is not None and intent.method == 'locker_credit'):
        credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=person)
        LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') + amount)
        record_transaction(person, description, 'refund', -amount, 'Locker credit', ref, refund_of=refund_of)
        return 'credit'
    if intent is not None and intent.moyasar_payment_id and intent.amount == amount:
        try:
            refund_intent(intent, reason=description, quiet=True)
        except (MoyasarError, PaymentError) as exc:
            raise RefundError('We couldn’t refund your card right now. Try again in a few minutes or ask the '
                              'front desk.') from exc
        return 'card'
    return 'desk'


def refund_words(where, amount):
    """The member-facing sentence for where a refund went."""
    if where == 'credit':
        return bi(f'SAR {amount} was refunded to your locker credit.', f'أُعيد {amount} ريال إلى رصيد خزانتك.')
    if where == 'card':
        return bi(f'SAR {amount} will be refunded to your card (3–10 days to appear).',
                  f'سيُعاد {amount} ريال إلى بطاقتك (يظهر خلال 3–10 أيام).')
    return bi(f'The front desk will refund your SAR {amount}.', f'سيعيد لك مكتب الاستقبال مبلغ {amount} ريال.')
