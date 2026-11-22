"""
Deleting a member (Admin → Members → Delete).

A member with no payment, invoice, payment attempt or shop order is deleted completely, with their
signup. Anyone who paid has tax records that must be kept (ZATCA), so their account is closed and
everything personal is wiped instead: name, email, phone, photo, Iqama, birthday, the signup photo and
address, workouts, meals, visits, face checks, plans, reviews, notifications, saved cards and sign-in
devices. Their payments and invoices stay, under "Deleted member #<id>".
"""
import logging

from django.db import transaction
from django.db.models import ProtectedError

logger = logging.getLogger(__name__)

# Kept for the tax records; everything else about the member goes.
FINANCIAL = ('payments.Transaction', 'payments.Invoice', 'payments.PaymentIntent', 'shop.Order')


def has_financial_records(user):
    from django.apps import apps
    return any(apps.get_model(label)._base_manager.filter(member=user).exists() for label in FINANCIAL)


def _delete_file(field):
    try:
        if field:
            field.delete(save=False)
    except Exception:   # noqa: BLE001 — a missing file must not stop the erase
        logger.warning('Could not delete file %s', getattr(field, 'name', field))


def _erase_registration(reg, user_id):
    _delete_file(reg.photo)
    reg.full_name = 'Deleted member'
    reg.email = f'deleted-{user_id}-{reg.id}@deleted.invalid'
    reg.phone = ''
    reg.address = ''
    reg.address_lat = reg.address_lng = None
    reg.iqama_number = f'DEL{reg.id}'[:20]          # unique, and no longer a real Iqama
    reg.password_hash = ''
    reg.payment_raw = None
    reg.save()


def _personal_rows(user):
    """Querysets of the member's personal data (deleted on erase); tax-relevant rows are not listed."""
    from apps.accounts.models import TrustedDevice, TwoFactor, TwoFactorChallenge
    from apps.attendance.models import MemberMilestone
    from apps.catalog.models import CourseReview, Enrollment, TrainerReview, TrainerSession
    from apps.classes.models import ClassEnrollment, ClassPass
    from apps.facilities.models import FacilityBooking
    from apps.members.models import CheckIn, FaceCheck, MembershipFacility, Referral
    from apps.notifications.models import Notification, NotificationPreference
    from apps.nutrition.models import DexaScan, MealLog, NutritionPlan, WaterIntake
    from apps.payments.models import PaymentMethod
    from apps.shop.models import Cart
    from apps.workouts.models import PersonalRecord, VideoPurchase, Workout
    from django.db.models import Q
    b = lambda m: m._base_manager     # noqa: E731 — both gym sides
    return [
        b(TwoFactor).filter(user=user), b(TwoFactorChallenge).filter(user=user), b(TrustedDevice).filter(user=user),
        b(CheckIn).filter(member=user), b(FaceCheck).filter(member=user), b(MemberMilestone).filter(member=user),
        b(Workout).filter(member=user), b(PersonalRecord).filter(member=user),
        b(MealLog).filter(member=user), b(WaterIntake).filter(member=user), b(DexaScan).filter(member=user),
        b(NutritionPlan).filter(member=user),
        b(CourseReview).filter(member=user), b(TrainerReview).filter(member=user),
        b(Notification).filter(recipient=user), b(NotificationPreference).filter(user=user),
        b(PaymentMethod).filter(member=user), b(Cart).filter(member=user),
        b(ClassEnrollment).filter(member=user), b(FacilityBooking).filter(member=user),
        b(Referral).filter(Q(referrer=user) | Q(referred=user)),
        # Paid things: deleted when no payment points at them, kept otherwise.
        b(Enrollment).filter(member=user), b(TrainerSession).filter(member=user), b(ClassPass).filter(member=user),
        b(VideoPurchase).filter(member=user), b(MembershipFacility).filter(member=user),
    ]


def erase_member(user, by):
    """'deleted' (gone completely) or 'erased' (closed and anonymised; payments kept)."""
    from apps.catalog.reviews import refresh_course_rating, refresh_trainer_rating
    from apps.catalog.models import CourseReview, TrainerReview
    from apps.members.models import Membership, MembershipRegistration
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    uid, email = user.id, user.email
    courses = {r.course for r in CourseReview._base_manager.filter(member=user).select_related('course')}
    trainers = {r.trainer for r in TrainerReview._base_manager.filter(member=user).select_related('trainer')}
    regs = list(MembershipRegistration._base_manager.filter(created_user=user))
    with transaction.atomic():
        for t in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=t)       # signs them out everywhere
        if not has_financial_records(user):
            _delete_file(user.avatar)
            for reg in regs:
                _delete_file(reg.photo)
                reg.delete()
            user.delete()
            outcome = 'deleted'
        else:
            for qs in _personal_rows(user):
                try:
                    with transaction.atomic():
                        qs.delete()
                except ProtectedError:
                    pass                    # a payment still points at it: kept for the records
            Membership._base_manager.filter(member=user).update(iqama_number='', nfc_token='', auto_renew=False)
            for reg in regs:
                _erase_registration(reg, uid)
            _delete_file(user.avatar)
            user.email = user.username = f'deleted-{uid}@deleted.invalid'
            user.first_name, user.last_name = 'Deleted', f'member #{uid}'
            user.phone = None
            user.date_of_birth = None
            user.referral_code = None
            user.is_active = False
            user.set_unusable_password()
            user.save()
            outcome = 'erased'
    for c in courses:
        refresh_course_rating(c)
    for t in trainers:
        refresh_trainer_rating(t)
    logger.info('Member #%s (%s) %s by %s', uid, email, outcome, by.email)
    return outcome
