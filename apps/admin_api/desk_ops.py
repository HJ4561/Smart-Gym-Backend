"""
Front desk (receptionist portal) — the parts that aren't plain admin screens:

  GET  /api/admin/desk/today/                    counters for the desk home (only the modules they have)
  POST /api/admin/desk/walk-in/quote/            price of a plan (+ facilities) before taking the money
  POST /api/admin/desk/walk-in/                  sign up a person standing at the desk, paid there, approved at once
  POST /api/admin/members/<id>/verify-identity/  first visit: Iqama and photo checked in person

Signups made at the desk don't wait for an admin: the receptionist saw the person and took the payment.
Every write lands in the staff activity log (apps.approvals.activity) so the admin can audit it.
"""
import logging
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.accounts.models import User
from apps.core.roles import DeskModule, modules_of
from apps.members.models import CheckIn, Membership, MembershipPlan, MembershipRegistration
from apps.members.serializers import MembershipSerializer
from apps.public_api.serializers import RegistrationCreateSerializer, plan_amount

logger = logging.getLogger(__name__)

DESK_PAYMENTS = {'cash': 'Cash at desk', 'card_terminal': 'Card terminal at desk'}


# ============================================================
# Desk home
# ============================================================
@api_view(['GET'])
@permission_classes([DeskModule('registrations', 'gate', 'walk_in', 'members', 'classes', 'inbox')])
def desk_today(request):
    """GET /api/admin/desk/today/ — today's numbers for the modules this person has."""
    mods = set(modules_of(request.user))
    today = timezone.localdate()
    out = {'modules': sorted(mods)}
    if 'registrations' in mods:
        out['signups_waiting'] = MembershipRegistration.objects.filter(
            status=MembershipRegistration.STATUS_PAID).count() + MembershipRegistration.objects.filter(
            status=MembershipRegistration.STATUS_PENDING, payment_status='paid').count()
    if 'gate' in mods:
        out['checked_in_now'] = CheckIn.objects.filter(check_in_time__date=today, check_out_time__isnull=True).count()
        out['check_ins_today'] = CheckIn.objects.filter(check_in_time__date=today).count()
        out['first_visits_waiting'] = Membership.objects.filter(
            id_verified_at__isnull=True, total_check_ins=0, status='active', expiry_date__gt=today).count()
    if 'walk_in' in mods:
        out['walk_ins_today'] = MembershipRegistration.objects.filter(
            approved_at__date=today, payment_method__in=list(DESK_PAYMENTS)).count()
    if 'inbox' in mods:
        from apps.site_content.models import ContactMessage
        out['messages_new'] = ContactMessage.objects.filter(status='new').count()
    if 'classes' in mods:
        from apps.classes.models import GymClass
        out['classes_today'] = GymClass.objects.filter(day_of_week=today.weekday(), is_active=True).count()
    return Response(out)


# ============================================================
# First visit
# ============================================================
@api_view(['POST'])
@permission_classes([DeskModule('gate', 'walk_in')])
def verify_identity(request, member_id):
    """
    POST /api/admin/members/<id>/verify-identity/   {iqama_checked: true, photo_checked: true}
    The member's first visit: the desk compared the Iqama card and the face with the signup photo.
    Until this is done the member can't check in.
    """
    member = get_object_or_404(User, id=member_id, role='member')
    m = Membership.objects.filter(member=member).first()
    if m is None:
        return Response({'detail': 'Member has no membership.'}, status=status.HTTP_400_BAD_REQUEST)
    if request.data.get('iqama_checked') is not True or request.data.get('photo_checked') is not True:
        return Response({'detail': 'Check their Iqama card and their face against the photo, then tick both.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if m.id_verified_at is None:
        m.id_verified_at = timezone.now()
        m.id_verified_by = request.user
        m.save(update_fields=['id_verified_at', 'id_verified_by', 'updated_at'])
        logger.info('First-visit ID check for %s by %s', member.email, request.user.email)
    return Response({'ok': True, 'membership': MembershipSerializer(m).data})


# ============================================================
# Walk-in signup
# ============================================================
class WalkInSerializer(RegistrationCreateSerializer):
    """The website signup, filled in by the desk: no password (the member gets a temporary one by email)
    and no online face check (the receptionist takes the photo of the person in front of them)."""
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    payment = serializers.ChoiceField(choices=list(DESK_PAYMENTS), write_only=True,
                                      error_messages={'invalid_choice': 'Choose how they paid: cash or card terminal.',
                                                      'required': 'Choose how they paid: cash or card terminal.'})
    payment_reference = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=60)

    class Meta(RegistrationCreateSerializer.Meta):
        fields = RegistrationCreateSerializer.Meta.fields + ['payment', 'payment_reference']

    def validate_photo(self, value):
        from apps.core.photo_quality import photo_problem
        from apps.public_api.serializers import MAX_PHOTO_BYTES
        if value.size > MAX_PHOTO_BYTES:
            raise serializers.ValidationError('Photo must be under 4 MB.')
        problem = photo_problem(value)
        if problem:
            raise serializers.ValidationError(problem)
        return value

    def validate_password(self, value):
        return ''

    def create(self, validated_data):
        method = validated_data.pop('payment')
        ref = (validated_data.pop('payment_reference', '') or '').strip()
        validated_data.pop('password', None)
        from apps.members.facilities import quote_total
        validated_data.update(
            password_hash=make_password(None),        # unusable → approval emails a temporary password
            payment_amount=plan_amount(validated_data['plan'], validated_data.get('duration_months', 1))
            + quote_total(validated_data.get('facilities')),
            payment_method=method, payment_status='paid', payment_paid_at=timezone.now(),
            payment_failure_reason='', status=MembershipRegistration.STATUS_PAID,
        )
        reg = getattr(self, '_resume', None)
        if reg is None:
            reg = MembershipRegistration.objects.create(**validated_data)
        else:   # they started online and never paid: finish that signup here
            for field, value in validated_data.items():
                setattr(reg, field, value)
            reg.moyasar_payment_id = None
            reg.save()
        reg.payment_reference = f'desk:{reg.id}:{ref}'[:100] if ref else f'desk:{reg.id}:{timezone.now():%Y%m%d%H%M%S}'
        reg.save(update_fields=['payment_reference'])
        return reg


@api_view(['POST'])
@permission_classes([DeskModule('walk_in')])
def walk_in_quote(request):
    """POST /api/admin/desk/walk-in/quote/  {plan, duration_months, facilities} → {plan, facilities[], total}"""
    from apps.members.facilities import FacilityError, parse_selection, quote_total, signup_quote
    plan = MembershipPlan.objects.filter(id=request.data.get('plan'), is_active=True).first()
    if plan is None:
        return Response({'plan': ['Choose a plan.']}, status=status.HTTP_400_BAD_REQUEST)
    try:
        months = int(request.data.get('duration_months') or 1)
    except (TypeError, ValueError):
        months = 0
    if not 1 <= months <= 24:
        return Response({'duration_months': ['Duration must be between 1 and 24 months.']},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        lines = signup_quote(parse_selection(request.data.get('facilities'), months))
    except FacilityError as exc:
        return Response({'facilities': [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
    base = plan_amount(plan, months)
    return Response({'plan': str(base), 'facilities': lines, 'total': str(base + quote_total(lines)),
                     'currency': 'SAR'})


@api_view(['POST'])
@permission_classes([DeskModule('walk_in')])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def walk_in(request):
    """
    POST /api/admin/desk/walk-in/  multipart:
      full_name, email, phone, iqama_number, age, gender, address, photo (taken at the desk),
      plan, duration_months, facilities, payment (cash | card_terminal), payment_reference (terminal slip no.)
    Creates the account and membership straight away (no admin approval), records the payment with its
    receipt and ZATCA invoice, marks their ID as checked, and emails the login details.
    """
    ser = WalkInSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
    from apps.core.i18n import language
    from apps.members.services import approve_registration
    wanted = str(request.data.get('language') or '').lower()
    try:
        with transaction.atomic():
            reg = ser.save(language=wanted if wanted in ('ar', 'en') else language())
            user = approve_registration(reg, approved_by=request.user, paid_at_desk=True)
            m = Membership.objects.get(member=user)
            m.id_verified_at, m.id_verified_by = timezone.now(), request.user   # seen in person just now
            m.save(update_fields=['id_verified_at', 'id_verified_by', 'updated_at'])
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    logger.info('Walk-in signup %s by %s (%s, SAR %s)', user.email, request.user.email, reg.payment_method,
                reg.payment_amount)
    return Response({
        'ok': True,
        'detail': 'Member added. Their login details are on the way by email.',
        'user': {'id': user.id, 'email': user.email, 'full_name': user.full_name},
        'membership': MembershipSerializer(m).data,
        'amount': str(reg.payment_amount or Decimal('0')),
        'payment': DESK_PAYMENTS[reg.payment_method],
    }, status=status.HTTP_201_CREATED)
