import re
import secrets
from decimal import Decimal

from rest_framework import serializers
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from apps.members.models import (
    MembershipPlan, MembershipRegistration, Membership,
)
from .moyasar import sync_payment, MoyasarError, MoyasarUnavailable

IQAMA_RE = re.compile(r'^2\d{9}$')          # Saudi residency ID: 10 digits, starts with 2
MAX_PHOTO_BYTES = 4 * 1024 * 1024           # Vercel rejects request bodies over 4.5 MB


def plan_amount(plan, months):
    """Server-side price in SAR — the browser never decides how much to charge."""
    if months >= 12:
        return (Decimal(plan.price_annual) * months / 12).quantize(Decimal('0.01'))
    return (Decimal(plan.price_monthly) * months).quantize(Decimal('0.01'))


# ============================================================
# PUBLIC — READ
# ============================================================
class PublicPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = MembershipPlan
        fields = [
            'id', 'name', 'display_name', 'description',
            'price_monthly', 'price_annual', 'features', 'session_limit_minutes',
            'display_name_ar', 'description_ar', 'features_ar', 'video_access',
        ]


# ============================================================
# PUBLIC — REGISTER
# ============================================================
class RegistrationCreateSerializer(serializers.ModelSerializer):
    """
    Accepts multipart/form-data from the public site:
      iqama_number, full_name, email, phone, address, age, gender,
      photo (file, required), liveness_token, plan (id), duration_months, password

    The photo must come from a passed live face check (apps.members.liveness) — a photo of a
    photo is refused.

    The password becomes the member's login when an admin approves the signup.

    Resubmitting with the same Iqama + email as an existing *unpaid* pending
    registration (e.g. after a declined card) updates it instead of failing.
    """
    plan = serializers.PrimaryKeyRelatedField(
        queryset=MembershipPlan.objects.filter(is_active=True),
    )
    password = serializers.CharField(write_only=True, min_length=8)
    # Declared explicitly so DRF doesn't add UniqueValidators; validate() handles duplicates.
    iqama_number = serializers.CharField(max_length=20)
    email = serializers.EmailField(max_length=254)
    photo = serializers.ImageField()
    # Optional facility add-ons from the admin "Facilities" list: "3,7" (whole plan) or "3:6,7:1" (months each).
    facilities = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = MembershipRegistration
        fields = [
            'iqama_number', 'full_name', 'email', 'phone',
            'address', 'age', 'gender', 'photo',
            'plan', 'duration_months', 'password', 'facilities',
            'address_lat', 'address_lng',
        ]

    def validate_phone(self, value):
        from apps.core.phone import MESSAGE, normalize_saudi_mobile
        phone = normalize_saudi_mobile(value)
        if phone is None:
            raise serializers.ValidationError(MESSAGE)
        return phone

    def validate_address_lat(self, value):
        if value is not None and not -90 <= value <= 90:
            raise serializers.ValidationError('Invalid latitude.')
        return value

    def validate_address_lng(self, value):
        if value is not None and not -180 <= value <= 180:
            raise serializers.ValidationError('Invalid longitude.')
        return value

    def validate_iqama_number(self, value):
        value = value.strip()
        if not IQAMA_RE.match(value):
            raise serializers.ValidationError('Iqama number must be 10 digits starting with 2.')
        return value

    def validate_email(self, value):
        from apps.accounts.models import User
        from apps.core.validators import email_error
        value = value.strip().lower()
        msg = email_error(value)
        if msg:
            raise serializers.ValidationError(msg)
        # An account that isn't from a pending signup (staff, trainer, existing member) owns this email.
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('This email already has a Smart Gym account. Sign in to the member portal instead.')
        return value

    def validate_full_name(self, value):
        from apps.core.validators import person_name_error
        value = ' '.join(value.split())
        msg = person_name_error(value, 'Full name')
        if msg:
            raise serializers.ValidationError(msg)
        if len(value.split(' ')) < 2:
            raise serializers.ValidationError('Enter your first and last name.')
        return value

    def validate_photo(self, value):
        from apps.core.photo_quality import photo_problem
        if value.size > MAX_PHOTO_BYTES:
            raise serializers.ValidationError('Photo must be under 4 MB.')
        problem = photo_problem(value)
        if problem:
            raise serializers.ValidationError(problem)
        from apps.members.liveness import registration_problem
        problem = registration_problem(self.initial_data.get('liveness_token'), value)
        if problem:
            raise serializers.ValidationError(problem)
        return value

    def validate(self, attrs):
        # Price the facility add-ons now (months = membership length); prices are frozen on the registration.
        from apps.members.facilities import FacilityError, parse_selection, signup_quote
        try:
            months = attrs.get('duration_months', 1)
            attrs['facilities'] = signup_quote(parse_selection(attrs.get('facilities'), months))
        except FacilityError as exc:
            raise serializers.ValidationError({'facilities': str(exc)})

        by_iqama = MembershipRegistration.objects.filter(iqama_number=attrs['iqama_number']).first()
        by_email = MembershipRegistration.objects.filter(email__iexact=attrs['email']).first()
        existing = by_iqama or by_email
        # The same person under a new email: a live signup already uses this full name.
        same_name = (MembershipRegistration.objects.filter(full_name__iexact=attrs['full_name'])
                     .exclude(status=MembershipRegistration.STATUS_REJECTED))
        if existing is not None:
            same_name = same_name.exclude(id=existing.id)
        if same_name.exists():
            raise serializers.ValidationError(
                {'full_name': 'A member with this name is already registered. If that’s you, sign in to the member '
                              'portal; otherwise add your middle or family name.'})
        if existing is None:
            return attrs

        resumable = (
            by_iqama == by_email
            and existing.status == MembershipRegistration.STATUS_PENDING
            and existing.payment_status != 'paid'
        )
        if resumable and existing.moyasar_payment_id:
            # Make sure the last attempt didn't succeed while the user was away.
            try:
                existing = sync_payment(existing.moyasar_payment_id)
            except MoyasarUnavailable:
                # Can't rule out that the last attempt succeeded — don't discard it.
                raise serializers.ValidationError(
                    {'detail': 'Could not check your previous payment. Please try again shortly.'}
                )
            except MoyasarError:
                pass
            resumable = (
                existing.status == MembershipRegistration.STATUS_PENDING
                and existing.payment_status != 'paid'
            )

        if not resumable:
            errors = {}
            if by_iqama:
                errors['iqama_number'] = 'This Iqama number is already registered.'
            if by_email:
                errors['email'] = 'This email is already registered.'
            raise serializers.ValidationError(errors)

        self._resume = existing
        return attrs

    def validate_duration_months(self, value):
        if value < 1 or value > 24:
            raise serializers.ValidationError('Duration must be between 1 and 24 months.')
        return value

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))
        return value

    def create(self, validated_data):
        # Hash now; the real User is created from password_hash on approval.
        validated_data['password_hash'] = make_password(validated_data.pop('password'))
        from apps.members.facilities import quote_total
        validated_data['payment_amount'] = plan_amount(
            validated_data['plan'], validated_data.get('duration_months', 1),
        ) + quote_total(validated_data.get('facilities'))
        validated_data['payment_status'] = 'pending'
        validated_data['payment_failure_reason'] = ''
        validated_data['payment_attempt_token'] = secrets.token_urlsafe(24)  # new attempt
        validated_data['status'] = MembershipRegistration.STATUS_PENDING

        from apps.members.liveness import mark_used
        mark_used(self.initial_data.get('liveness_token'))
        reg = getattr(self, '_resume', None)
        if reg is None:
            return MembershipRegistration.objects.create(**validated_data)

        # Retry of an unpaid registration: overwrite it and start a fresh payment.
        old_photo = reg.photo.name if reg.photo else None
        for field, value in validated_data.items():
            setattr(reg, field, value)
        reg.moyasar_payment_id = None
        reg.save()
        if old_photo and old_photo != reg.photo.name:
            reg.photo.storage.delete(old_photo)
        return reg


class RegistrationStatusSerializer(serializers.ModelSerializer):
    """
    Public, unauthenticated view (anyone can call it with any id), so it
    carries no personal data — only processing state.
    """
    class Meta:
        model = MembershipRegistration
        fields = ['id', 'status', 'payment_status', 'payment_failure_reason']


# ============================================================
# ADMIN — READ / MANAGE
# ============================================================
class AdminRegistrationSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.display_name', read_only=True)
    photo_url = serializers.SerializerMethodField()
    approved_by_email = serializers.EmailField(source='approved_by.email', read_only=True, allow_null=True)
    created_user_email = serializers.EmailField(source='created_user.email', read_only=True, allow_null=True)
    membership = serializers.SerializerMethodField()
    receipt_url = serializers.SerializerMethodField()

    class Meta:
        model = MembershipRegistration
        fields = [
            'id', 'receipt_url',
            'iqama_number', 'full_name', 'email', 'phone',
            'address', 'address_lat', 'address_lng', 'age', 'gender', 'photo_url',
            'plan', 'plan_name', 'duration_months',
            'payment_method', 'payment_reference', 'payment_amount', 'facilities',
            'payment_status', 'payment_paid_at',
            'moyasar_payment_id', 'payment_failure_reason',
            'status', 'submitted_at', 'approved_at',
            'approved_by_email', 'rejection_reason',
            'created_user_email', 'membership',
        ]

    def get_receipt_url(self, obj):
        from apps.payments.receipts import receipt_url
        return receipt_url('r', obj.id) if obj.payment_status in ('paid', 'refunded') else None

    def get_membership(self, obj):
        """For approved signups: the membership period that started at approval."""
        m = getattr(obj.created_user, 'membership', None) if obj.created_user_id else None
        if m is None:
            return None
        return {
            'start_date': m.start_date,
            'expiry_date': m.expiry_date,
            'status': m.status,
            'time_remaining': m.time_remaining,
        }

    def get_photo_url(self, obj):
        if not obj.photo:
            return None
        request = self.context.get('request')
        url = obj.photo.url
        return request.build_absolute_uri(url) if request else url


class BulkApproveSerializer(serializers.Serializer):
    """Body for POST /api/admin/registrations/bulk-approve/"""
    ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text='Optional list of IDs. If omitted, approves all pending.',
    )
    only_today = serializers.BooleanField(
        default=False,
        help_text='If true, restricts to registrations submitted today.',
    )


class PaymentIdSerializer(serializers.Serializer):
    """Body for the attach / verify endpoints: the Moyasar payment id."""
    payment_id = serializers.CharField(max_length=64)


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')
    refund = serializers.BooleanField(required=False, default=False)