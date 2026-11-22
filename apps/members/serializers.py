# apps/members/serializers.py
from rest_framework import serializers
from .models import MembershipPlan, Membership, CheckIn


class MembershipPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = MembershipPlan
        fields = [
            'id', 'name', 'display_name', 'description',
            'price_monthly', 'price_annual', 'features', 'session_limit_minutes', 'is_active',
        ]


class MembershipSerializer(serializers.ModelSerializer):
    plan = MembershipPlanSerializer(read_only=True)
    member_name = serializers.SerializerMethodField()
    days_remaining = serializers.IntegerField(read_only=True)
    # {months, days, total_days_remaining, plan_total_days, percent_used, expired} — counted from approval
    time_remaining = serializers.DictField(read_only=True)
    needs_first_visit_check = serializers.BooleanField(read_only=True)

    class Meta:
        model = Membership
        fields = [
            'id', 'plan', 'status', 'start_date', 'expiry_date',
            'auto_renew', 'is_frozen', 'frozen_until',
            'pass_id', 'nfc_token',
            'lifetime_value', 'total_check_ins',
            'days_remaining', 'time_remaining', 'member_name',
            'id_verified_at', 'needs_first_visit_check',
            'created_at', 'updated_at',
        ]

    def get_member_name(self, obj):
        u = obj.member
        # Try full_name property, fallback to first+last, fallback to email
        if hasattr(u, 'full_name') and u.full_name:
            return u.full_name
        name = f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}".strip()
        return name or u.email


class CheckInSerializer(serializers.ModelSerializer):
    class Meta:
        model = CheckIn
        fields = [
            'id', 'facility', 'zone',
            'check_in_time', 'check_out_time', 'duration_minutes',
            'session_limit_minutes', 'expected_checkout_time', 'face_verified',
        ]
        read_only_fields = ['id', 'check_in_time', 'session_limit_minutes', 'expected_checkout_time']