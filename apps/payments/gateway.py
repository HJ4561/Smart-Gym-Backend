"""
Member payments: locker top-ups, shop orders, trainer sessions, paid courses,
membership renewals, facility add-ons, nutrition plans, Machine Guide videos and paid classes.

Flow (same rules as the registration payment in apps.public_api.moyasar):
  1. create_intent()      server decides the amount; returns a PaymentIntent
  2. browser pays with the Moyasar form (metadata = intent id + attempt token),
     or pay_with_credit() takes it from the member's locker credit
  3. sync_intent()        re-fetches the payment from Moyasar with the secret key
                          (callback page, attach call and webhook all land here)
  4. _fulfil()            marks the order paid / books the session / enrols /
                          extends the membership / adds credit, and records a
                          Transaction + Invoice + notification.
Nothing the browser or webhook body says about a payment is trusted.
"""
import hmac
import logging
import secrets
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.notifications.service import notify
from apps.public_api import moyasar
from apps.public_api.moyasar import CURRENCY, PAID_STATUSES, MoyasarError, _redact

from .models import Invoice, LockerCredit, PaymentIntent, Transaction

logger = logging.getLogger(__name__)

TOPUP_MIN = Decimal('10.00')
RENEWAL_MONTHS = (1, 3, 6, 12)
CREDIT_PURPOSES = ('shop_order', 'trainer_session', 'course', 'renewal', 'facility_addon', 'nutrition_plan',
                   'video', 'class_pass')


class PaymentError(Exception):
    """The request can't be paid as asked (shown to the member)."""


# ============================================================
# 1. CREATE
# ============================================================
def create_intent(member, purpose, data):
    """Validate what is being bought and price it. Raises PaymentError."""
    builders = {
        'locker_topup': _build_topup,
        'shop_order': _build_order,
        'trainer_session': _build_session,
        'course': _build_course,
        'renewal': _build_renewal,
        'facility_addon': _build_facility,
        'nutrition_plan': _build_nutrition,
        'video': _build_video,
        'class_pass': _build_class_pass,
    }
    if purpose not in builders:
        raise PaymentError('Unknown payment purpose.')
    fields = builders[purpose](member, data)
    if fields['amount'] <= 0:
        raise PaymentError('Nothing to pay for.')
    return PaymentIntent.objects.create(
        member=member, purpose=purpose, attempt_token=secrets.token_urlsafe(24), **fields,
    )


def _build_topup(member, data):
    try:
        amount = Decimal(str(data.get('amount'))).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise PaymentError('Enter a valid amount.')
    credit, _ = LockerCredit.objects.get_or_create(member=member)
    room = credit.max_cap - credit.balance
    if amount < TOPUP_MIN:
        raise PaymentError(f'The minimum top-up is SAR {TOPUP_MIN}.')
    if amount > room:
        raise PaymentError(f'Your locker credit is capped at SAR {credit.max_cap}; you can add up to SAR {max(room, 0)}.')
    return {'amount': amount, 'description': f'Locker credit top-up (SAR {amount})'}


def _build_order(member, data):
    from apps.shop.models import Order
    order = Order.objects.filter(order_number=data.get('order_number'), member=member).first()
    if order is None:
        raise PaymentError('Order not found.')
    if order.status != 'pending':
        raise PaymentError('This order is not awaiting payment.')
    return {'amount': order.total, 'order': order, 'description': f'Shop order {order.order_number}'}


def _build_session(member, data):
    from apps.catalog.models import TrainerSession
    session = TrainerSession.objects.select_related('trainer__user').filter(
        id=data.get('session_id'), member=member).first()
    if session is None:
        raise PaymentError('Session not found.')
    if session.status != 'pending_payment':
        raise PaymentError('This session is not awaiting payment.')
    if session.scheduled_at <= timezone.now():
        raise PaymentError('This session time has passed — please book another slot.')
    name = session.trainer.user.full_name or 'trainer'
    when = timezone.localtime(session.scheduled_at).strftime('%d %b %Y %H:%M')
    return {'amount': session.amount_charged, 'trainer_session': session,
            'description': f'Session with {name} — {when}'}


def _build_nutrition(member, data):
    from apps.nutrition.models import NutritionPlan
    plan = NutritionPlan.objects.select_related('nutritionist__user').filter(
        id=data.get('plan_id'), member=member).first()
    if plan is None:
        raise PaymentError('Plan request not found.')
    if plan.status != 'pending_payment':
        raise PaymentError('This plan is not awaiting payment.')
    name = plan.nutritionist.user.full_name or 'nutritionist'
    return {'amount': plan.price, 'nutrition_plan': plan, 'description': f'Nutrition plan with {name}'}


def _build_course(member, data):
    from apps.catalog.models import Course, Enrollment
    course = Course.objects.filter(id=data.get('course_id'), status='published').first()
    if course is None:
        raise PaymentError('Course not found.')
    if Enrollment.objects.filter(member=member, course=course).exists():
        raise PaymentError('You are already enrolled in this course.')
    if course_is_free_for(member, course):
        raise PaymentError('This course is free for you — enrol directly.')
    return {'amount': course.price, 'course': course, 'description': f'Course: {course.title}'}


def _build_renewal(member, data):
    from apps.members.models import MembershipPlan
    from apps.public_api.serializers import plan_amount
    try:
        months = int(data.get('months'))
    except (TypeError, ValueError):
        raise PaymentError('Choose a duration.')
    if months not in RENEWAL_MONTHS:
        raise PaymentError('Choose 1, 3, 6 or 12 months.')
    plan = MembershipPlan.objects.filter(id=data.get('plan_id'), is_active=True).first()
    if plan is None:
        raise PaymentError('Plan not found.')
    from apps.members.models import Membership
    if not Membership.objects.filter(member=member).exists():
        raise PaymentError('No membership to renew.')
    return {'amount': plan_amount(plan, months), 'plan': plan, 'months': months,
            'description': f'Membership renewal — {plan.display_name} ({months} mo)'}


def _build_facility(member, data):
    from apps.members.facilities import FacilityError, addon_quote
    try:
        item_id = int(data.get('facility_id'))
    except (TypeError, ValueError):
        raise PaymentError('Choose a facility.')
    try:
        addon, months, amount = addon_quote(member, item_id)
    except FacilityError as exc:
        raise PaymentError(str(exc))
    return {'amount': amount, 'facility_item_id': item_id, 'months': months,
            'description': f'Facility add-on — {addon.name} ({months} mo)'}


def _build_video(member, data):
    from apps.workouts import videos
    from apps.workouts.models import ExerciseVideo
    video = ExerciseVideo.objects.select_related('exercise').filter(id=data.get('video_id'), is_active=True).first()
    if video is None:
        raise PaymentError('Video not found.')
    can, why = videos.access(member, video)
    if can:
        raise PaymentError('You can already watch this video' + (' — it’s included in your plan.' if why == 'plan' else '.'))
    return {'amount': video.price, 'video': video,
            'description': f'Video: {video.title} ({video.exercise.name})'[:200]}


def _build_class_pass(member, data):
    from apps.classes import passes
    from apps.classes.models import GymClass
    c = GymClass.objects.select_related('trainer__user').filter(id=data.get('class_id')).first()
    if c is None:
        raise PaymentError('Class not found.')
    kind = data.get('kind')
    day = passes.parse_day(data.get('date')) if kind == 'session' else None
    try:
        price, until = passes.check_purchase(member, c, kind, day)
    except ValueError as exc:
        raise PaymentError(str(exc))
    what = f'{c.name} — whole class until {until:%d %b %Y}' if kind == 'full' else f'{c.name} — {day:%a %d %b %Y}'
    return {'amount': price, 'gym_class': c, 'pass_kind': kind, 'session_date': day, 'description': f'Class: {what}'}


def course_is_free_for(member, course):
    """Free courses, and 'included with Pro' courses for members on a Pro/Elite plan."""
    if not course.price or course.price <= 0:
        return True
    if course.is_included_with_pro:
        from apps.members.models import Membership
        m = Membership.objects.select_related('plan').filter(member=member).first()
        if m and m.allows_entry and any(t in m.plan.name.lower() for t in ('pro', 'elite')):
            return True
    return False


# ============================================================
# 2. MOYASAR FORM PARAMETERS
# ============================================================
def checkout_params(intent):
    return {
        'publishable_key': settings.MOYASAR_PUBLISHABLE_KEY,
        'amount': intent.amount_halalas,
        'currency': CURRENCY,
        'description': f'Smart Gym — {intent.description}'[:255],
        'metadata': {'intent_id': str(intent.id), 'attempt': intent.attempt_token},
    }


# ============================================================
# 3. VERIFY WITH MOYASAR
# ============================================================
def attach_payment(intent, payment_id):
    """Link the Moyasar payment the browser just created, then verify it."""
    if not isinstance(payment_id, str) or not payment_id or len(payment_id) > 64:
        raise MoyasarError('Invalid payment id.')
    return sync_intent(payment_id, intent_id=intent.id)


def sync_intent(payment_id, intent_id=None, payment=None):
    """Idempotent: safe to call from the callback page, attach and the webhook in any order."""
    payment = payment or moyasar.fetch_payment(payment_id)
    metadata = payment.get('metadata') or {}
    meta_id = str(metadata.get('intent_id', ''))
    if not meta_id.isdigit():
        raise MoyasarError('Payment is not linked to a member payment.')
    if intent_id is not None and meta_id != str(intent_id):
        raise MoyasarError('Payment does not belong to this payment request.')

    with transaction.atomic():
        intent = PaymentIntent.objects.select_for_update().filter(id=int(meta_id)).first()
        if intent is None:
            raise MoyasarError('Payment request not found.')
        attempt = metadata.get('attempt')
        if not (isinstance(attempt, str) and hmac.compare_digest(attempt.encode(), intent.attempt_token.encode())):
            raise MoyasarError('Payment does not match this payment request.')
        _apply_payment(intent, payment)
    return intent


def _apply_payment(intent, payment):
    pid = payment.get('id')
    status = payment.get('status')
    if intent.status in ('paid', 'review'):
        if pid != intent.moyasar_payment_id and status in PAID_STATUSES:
            logger.warning('Intent #%s already settled by %s — extra paid payment %s needs a manual refund.',
                           intent.id, intent.moyasar_payment_id, pid)
        return
    if PaymentIntent.objects.filter(moyasar_payment_id=pid).exclude(id=intent.id).exists():
        raise MoyasarError('Payment already used for another purchase.')

    intent.moyasar_payment_id = pid
    intent.payment_raw = _redact(payment)
    source = payment.get('source') or {}

    if status in PAID_STATUSES:
        problems = []
        if payment.get('amount') != intent.amount_halalas:
            problems.append(f"amount {payment.get('amount')} != expected {intent.amount_halalas} halalas")
        if payment.get('currency') != CURRENCY:
            problems.append(f"currency {payment.get('currency')} != {CURRENCY}")
        if problems:
            intent.status = 'review'
            intent.failure_reason = ('Needs manual review: ' + '; '.join(problems))[:255]
            logger.error('Intent #%s payment %s mismatch: %s', intent.id, pid, problems)
            intent.save()
            return
        _fulfil(intent, _source_label(source))
    elif status == 'failed':
        intent.status = 'failed'
        intent.failure_reason = (source.get('message') or 'Payment failed.')[:255]
        intent.save()
    else:
        intent.save()   # initiated / authorized (3-D Secure in progress)


def _source_label(source):
    company = (source.get('company') or source.get('type') or 'card').upper()
    number = str(source.get('number') or '')
    return f'{company} •••• {number[-4:]}' if number[-4:].isdigit() else company


# ============================================================
# 3b. PAY WITH LOCKER CREDIT
# ============================================================
def pay_with_credit(intent):
    if intent.purpose not in CREDIT_PURPOSES:
        raise PaymentError('Locker credit can’t be used for this payment.')
    with transaction.atomic():
        intent = PaymentIntent.objects.select_for_update().get(id=intent.id)
        if intent.status != 'pending':
            raise PaymentError('This payment is no longer pending.')
        credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=intent.member)
        if credit.balance < intent.amount:
            raise PaymentError(f'Not enough locker credit (balance SAR {credit.balance}).')
        LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') - intent.amount)
        intent.method = 'locker_credit'
        _fulfil(intent, 'Locker credit')
    return intent


# ============================================================
# 4. FULFIL
# ============================================================
TX_TYPE = {'locker_topup': 'topup', 'shop_order': 'retail', 'trainer_session': 'coaching',
           'course': 'coaching', 'renewal': 'subscription', 'facility_addon': 'subscription',
           'nutrition_plan': 'coaching', 'video': 'coaching', 'class_pass': 'coaching'}


def _fulfil(intent, method_label):
    """Runs inside the caller's transaction with the intent row locked."""
    ok, note = {
        'locker_topup': _fulfil_topup,
        'shop_order': _fulfil_order,
        'trainer_session': _fulfil_session,
        'course': _fulfil_course,
        'renewal': _fulfil_renewal,
        'facility_addon': _fulfil_facility,
        'nutrition_plan': _fulfil_nutrition,
        'video': _fulfil_video,
        'class_pass': _fulfil_class_pass,
    }[intent.purpose](intent)

    now = timezone.now()
    intent.paid_at = now
    intent.transaction = record_transaction(
        intent.member, intent.description, TX_TYPE[intent.purpose], intent.amount,
        method_label, intent.moyasar_payment_id or f'credit:{intent.id}',
    )
    if ok:
        intent.status = 'paid'
        intent.failure_reason = ''
    else:
        # Paid, but what it paid for is gone (order cancelled, slot taken...): staff refunds it.
        intent.status = 'review'
        intent.failure_reason = f'Needs manual review: {note}'[:255]
        logger.error('Intent #%s paid but not fulfilled: %s', intent.id, note)
        notify(intent.member, 'payment', 'Payment received — our team will follow up',
               f'We received SAR {intent.amount} for "{intent.description}" but {note}. '
               'Our team will contact you to rebook or refund.', icon='report', priority='high',
               action_label='View payments', action_path='/dashboard/payments')
    intent.save()


def record_transaction(member, description, tx_type, amount, method_label, gateway_ref, when=None, email=True,
                       invoice=True, refund_of=None):
    """
    A settled Transaction + its paid Invoice (shown on the member's Payments page). A payment
    (amount > 0) also emails the member a bilingual receipt once the database commit lands.

    ZATCA: once committed, a sale gets its signed tax invoice (`invoice=False` when one already
    exists, e.g. a signup invoiced at payment) and a refund of `refund_of` gets a credit note.
    """
    when = when or timezone.now()
    txn = Transaction.objects.create(
        member=member, transaction_id=f'TXN-{uuid.uuid4().hex[:10].upper()}',
        description=description[:200], transaction_type=tx_type,
        amount=amount, subtotal=amount, status='settled',
        payment_method_label=method_label[:60], gateway_reference=(gateway_ref or '')[:100],
        settled_at=when,
    )
    Invoice.objects.create(
        invoice_number=f'INV-{uuid.uuid4().hex[:10].upper()}', member=member, transaction=txn,
        description=description[:200], amount=amount, total=amount, status='paid', paid_at=when,
    )
    from apps.zatca import service as zatca
    if amount > 0 and invoice:
        zatca.after_commit(zatca.on_sale, txn.id)
    elif amount < 0 and refund_of is not None:
        zatca.after_commit(zatca.on_refund, refund_of.id, txn.id)
    if email and amount > 0:
        from .receipts import email_after_commit, from_transaction
        email_after_commit(from_transaction, txn)
    return txn


def paid_transaction(**filters):
    """The ledger entry of the paid intent matching `filters` (what a later refund reverses), or None."""
    intent = PaymentIntent.objects.filter(status='paid', transaction__isnull=False, **filters).order_by('-paid_at').first()
    return intent.transaction if intent else None


def _fulfil_topup(intent):
    credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=intent.member)
    # Money was taken, so it is always credited in full (even if another top-up raced past the cap).
    LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') + intent.amount)
    notify(intent.member, 'payment', 'Locker credit added', f'SAR {intent.amount} was added to your locker credit.',
           icon='account_balance_wallet', action_label='View balance', action_path='/dashboard/payments')
    return True, ''


def _fulfil_order(intent):
    from apps.shop.models import Order
    from apps.shop.services import EXPIRED_NOTE, retake_stock
    order = Order.objects.select_for_update().filter(id=intent.order_id).first()
    expired = order is not None and order.status == 'cancelled' and EXPIRED_NOTE in order.notes
    if expired and not retake_stock(order):
        return False, f'order {order.order_number} expired and an item has since sold out'
    if order is None or (order.status != 'pending' and not expired):
        return False, f'order {getattr(order, "order_number", "?")} is no longer awaiting payment'
    order.status = 'paid'
    order.save(update_fields=['status'])
    where = 'the counter' if order.delivery_method == 'counter' else f'locker {order.locker_number}'
    notify(intent.member, 'shop', f'Order {order.order_number} paid',
           f'Thanks! We are preparing your order — collect it from {where}.', icon='shopping_bag',
           action_label='View orders', action_path='/dashboard/shop?tab=orders')
    return True, ''


def _fulfil_session(intent):
    from apps.catalog.models import TrainerSession
    from apps.catalog.slots import slot_is_free
    session = TrainerSession.objects.select_for_update().select_related('trainer__user').filter(
        id=intent.trainer_session_id).first()
    if session is None or session.status != 'pending_payment':
        return False, 'the session is no longer awaiting payment'
    if not slot_is_free(session.trainer, session.scheduled_at, session.duration_minutes, exclude_id=session.id):
        session.status = 'cancelled'
        session.save(update_fields=['status'])
        return False, 'the time slot was booked by someone else'
    confirm_session(session)
    return True, ''


def confirm_session(session):
    """The session is on: paid, or free (the trainer's rate is 0). Tells the member and the coach."""
    session.status = 'scheduled'
    session.save(update_fields=['status'])
    when = timezone.localtime(session.scheduled_at).strftime('%A %d %b, %H:%M')
    notify(session.member, 'trainer', 'Session booked',
           f'Your session with {session.trainer.user.full_name} is confirmed for {when}.', icon='event_available',
           action_label='My sessions', action_path='/dashboard/my-sessions')
    from apps.notifications.service import notify_coach
    notify_coach(session.trainer, 'trainer', 'New 1-on-1 booking',
                 f'{session.member.full_name or "A member"} booked a session with you on {when}.', icon='event_available',
                 action_label='My sessions', action_path='/coach/sessions')


def _fulfil_nutrition(intent):
    from apps.nutrition.models import NutritionPlan
    plan = NutritionPlan.objects.select_for_update().select_related('nutritionist__user').filter(
        id=intent.nutrition_plan_id).first()
    if plan is None or plan.status != 'pending_payment':
        return False, 'the plan request is no longer awaiting payment'
    activate_plan(plan)
    return True, ''


def activate_plan(plan):
    """
    Start a requested plan. Plans with other nutritionists stay active — a member can work with
    several nutritionists they've paid. Caller holds a transaction.
    """
    from apps.nutrition.models import NutritionPlan
    NutritionPlan.objects.filter(member=plan.member, status='active', nutritionist=plan.nutritionist)         .exclude(id=plan.id).update(status='archived')
    plan.status = 'active'
    plan.save(update_fields=['status', 'updated_at'])
    name = plan.nutritionist.user.full_name or 'your nutritionist'
    notify(plan.member, 'nutrition', 'Nutrition plan started',
           f'Your plan with {name} is active. Starter targets are set; {name} will review and adjust them.',
           icon='restaurant', action_label='Open my plan', action_path='/dashboard/nutrition-plan')


def _fulfil_course(intent):
    from apps.catalog.models import Enrollment, enrollment_started
    if intent.course is None:
        return False, 'the course no longer exists'
    if Enrollment.objects.filter(member=intent.member, course=intent.course).exists():
        return False, 'you were already enrolled in this course'
    enrollment = Enrollment.objects.create(member=intent.member, course=intent.course, amount_paid=intent.amount,
                                           payment_ref=intent.moyasar_payment_id or f'credit:{intent.id}')
    type(intent.course).objects.filter(id=intent.course_id).update(total_enrolled=F('total_enrolled') + 1)
    enrollment_started(enrollment)
    notify(intent.member, 'course', 'Enrolled', f'You are enrolled in "{intent.course.title}". Enjoy!',
           icon='school', action_label='Open player', action_path=f'/dashboard/player/{intent.course_id}')
    return True, ''


def _fulfil_video(intent):
    from apps.notifications.service import notify_coach
    from apps.workouts.models import VideoPurchase
    video = intent.video
    if video is None:
        return False, 'the video was removed'
    _, created = VideoPurchase.objects.get_or_create(member=intent.member, video=video,
                                                     defaults={'amount': intent.amount})
    if not created:
        return False, 'you had already bought this video'
    notify(intent.member, 'workout', 'Video unlocked',
           f'“{video.title}” for {video.exercise.name} is yours to watch any time.', icon='smart_display',
           action_label='Watch now', action_path=f'/dashboard/machine-guide?exercise={video.exercise_id}')
    if video.coach_id:
        notify_coach(video.coach, 'workout', 'Someone bought your video',
                     f'{intent.member.full_name or "A member"} unlocked “{video.title}” (SAR {intent.amount}).',
                     icon='smart_display', action_label='My videos', action_path='/coach/videos')
    return True, ''


def _fulfil_class_pass(intent):
    from apps.classes import passes
    from apps.classes.models import ClassEnrollment, ClassPass, GymClass
    from apps.notifications.service import notify_coach
    c = GymClass.objects.select_for_update().filter(id=intent.gym_class_id).first()
    if c is None:
        return False, 'the class was removed'
    try:
        _, until = passes.check_purchase(intent.member, c, intent.pass_kind, intent.session_date)
    except ValueError as exc:
        return False, str(exc).rstrip('.').lower()
    ClassPass.objects.create(gym_class=c, member=intent.member, kind=intent.pass_kind,
                             session_date=intent.session_date, valid_until=until, amount=intent.amount)
    if intent.pass_kind == 'full':
        e, made = ClassEnrollment.objects.get_or_create(gym_class=c, member=intent.member,
                                                        defaults={'status': 'enrolled'})
        if not made:
            e.status = 'enrolled'
            e.save(update_fields=['status'])
        when = f'every {c.start_time:%H:%M} on its day until {until:%d %b %Y}'
    else:
        when = f'on {intent.session_date:%A %d %b} at {c.start_time:%H:%M}'
    notify(intent.member, 'attendance', 'Class booked', f'You’re in {c.name} {when}.', icon='event_available',
           action_label='My classes', action_path='/dashboard/classes')
    if c.trainer_id:
        notify_coach(c.trainer, 'attendance', 'New class booking',
                     f'{intent.member.full_name or "A member"} paid for {c.name} '
                     + ('(whole class).' if intent.pass_kind == 'full' else f'on {intent.session_date:%a %d %b}.'),
                     icon='groups', action_label='My classes', action_path='/coach/classes')
    return True, ''


def _fulfil_renewal(intent):
    from apps.members.models import Membership
    m = Membership.objects.select_for_update().filter(member=intent.member).first()
    if m is None or intent.plan is None:
        return False, 'there is no membership to renew'
    today = date.today()
    if m.expiry_date <= today:
        m.start_date = today                       # lapsed: the countdown restarts today
        base = today
    else:
        base = m.expiry_date
    m.plan = intent.plan
    m.expiry_date = base + relativedelta(months=intent.months)
    m.status = 'active'
    m.lifetime_value = (m.lifetime_value or 0) + intent.amount
    m.save()
    notify(intent.member, 'payment', 'Membership renewed',
           f'{intent.plan.display_name} is active until {m.expiry_date:%d %b %Y}.', icon='card_membership',
           action_label='View membership', action_path='/dashboard/payments')
    return True, ''


def _fulfil_facility(intent):
    from apps.members.facilities import active_ids, grant
    from apps.members.models import FacilityAddon, Membership
    m = Membership.objects.filter(member=intent.member).first()
    addon = FacilityAddon.objects.select_related('item').filter(item_id=intent.facility_item_id).first()
    if m is None or addon is None:
        return False, 'the facility or membership no longer exists'
    if intent.facility_item_id in active_ids(intent.member):
        return False, 'you already had access to this facility'
    grant(intent.member, intent.facility_item_id, addon.name, addon.monthly_price, intent.months,
          intent.amount, date.today(), m.expiry_date, 'addon')
    notify(intent.member, 'payment', 'Facility added',
           f'{addon.name} is included in your membership until {m.expiry_date:%d %b %Y}.', icon='pool',
           action_label='My facilities', action_path='/dashboard/payments')
    return True, ''


# ============================================================
# 5. REFUNDS (staff)
# ============================================================
def refund_intent(intent, reason='', staff=None):
    """
    Give the money of a paid / review intent back: to the card through Moyasar's
    refund API, or to locker credit when it was paid with credit. Records a
    negative Transaction. Raises PaymentError / MoyasarError.
    """
    with transaction.atomic():
        intent = PaymentIntent.objects.select_for_update().get(id=intent.id)
        if intent.status not in ('paid', 'review'):
            raise PaymentError('Only paid payments can be refunded.')
        if intent.purpose == 'locker_topup' and intent.status == 'paid':
            # The top-up was credited: take it back first (refused if already spent).
            credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=intent.member)
            if credit.balance < intent.amount:
                raise PaymentError(f'Only SAR {credit.balance} of this top-up is left in locker credit.')
            LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') - intent.amount)
        if intent.method == 'locker_credit':
            credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=intent.member)
            LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') + intent.amount)
            label = 'Locker credit'
        else:
            moyasar.refund_payment(intent.moyasar_payment_id, intent.amount_halalas)
            label = (intent.transaction.payment_method_label if intent.transaction_id else 'Card')
        record_transaction(intent.member, f'Refund — {intent.description}'[:200], 'refund', -intent.amount,
                           label, f'refund:{intent.moyasar_payment_id or intent.id}', refund_of=intent.transaction)
        intent.status = 'refunded'
        intent.failure_reason = (f'Refunded by {getattr(staff, "email", "staff")}: {reason}' if reason
                                 else f'Refunded by {getattr(staff, "email", "staff")}')[:255]
        intent.save(update_fields=['status', 'failure_reason'])
    notify(intent.member, 'payment', 'Refund issued',
           f'SAR {intent.amount} for "{intent.description}" was refunded'
           + (' to your locker credit.' if intent.method == 'locker_credit' else ' to your card (3–10 days to appear).'),
           icon='undo', action_label='View payments', action_path='/dashboard/payments')
    return intent
