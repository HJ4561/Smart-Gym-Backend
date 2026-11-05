from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from django.utils import timezone

from .models import MembershipPlan, Membership, CheckIn
from .serializers import (
    MembershipPlanSerializer,
    MembershipSerializer,
    CheckInSerializer,
)


# ============================================
# MEMBERSHIP PLANS (public)
# ============================================
@api_view(['GET'])
@permission_classes([AllowAny])
def plans_list(request):
    """
    GET /api/members/plans/
    Returns all active membership plans.
    """
    plans = MembershipPlan.objects.filter(is_active=True)
    return Response(MembershipPlanSerializer(plans, many=True).data)


# ============================================
# MY MEMBERSHIP
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_membership(request):
    """
    GET /api/members/membership/
    Returns the current user's membership.
    """
    try:
        membership = Membership.objects.get(member=request.user)
        return Response(MembershipSerializer(membership).data)
    except Membership.DoesNotExist:
        return Response(
            {'detail': 'You have no active membership.'},
            status=status.HTTP_404_NOT_FOUND,
        )


# ============================================
# CHECK-INS
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def check_ins(request):
    """
    GET /api/members/check-ins/ → the member's check-ins.
    Check-ins are recorded at the entrance (face gate / staff scanning the
    pass), never by the member, so attendance can't be faked.
    """
    entries = CheckIn.objects.filter(member=request.user)[:50]
    return Response(CheckInSerializer(entries, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_facilities(request):
    """
    GET /api/members/facilities/ → bookable facilities with ownership and the
    add-on price today (monthly price × months remaining). Buy one with
    POST /api/payments/checkout/ {purpose: "facility_addon", facility_id}.
    """
    from .facilities import member_view
    return Response(member_view(request.user))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_pass(request):
    """
    GET /api/members/pass/ → { token, expires_in, pass_id, valid }
    The QR on the member's digital pass. Staff scan it in Member Lookup.
    """
    from .passes import MAX_AGE_SECONDS, make_token
    membership = Membership.objects.select_related('plan').filter(member=request.user).first()
    if membership is None:
        return Response({'detail': 'No membership.'}, status=status.HTTP_404_NOT_FOUND)
    return Response({
        'token': make_token(membership),
        'expires_in': MAX_AGE_SECONDS,
        'refresh_in': 60,
        'pass_id': membership.pass_id,
        'valid': membership.allows_entry,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def check_out(request, check_in_id):
    """
    POST /api/members/check-ins/<id>/check-out/
    Closes an active check-in and calculates duration.
    """
    try:
        entry = CheckIn.objects.get(id=check_in_id, member=request.user)
    except CheckIn.DoesNotExist:
        return Response(
            {'detail': 'Check-in not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if entry.check_out_time:
        return Response(
            {'detail': 'Already checked out.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    entry.check_out_time = timezone.now()
    duration = (entry.check_out_time - entry.check_in_time).total_seconds() / 60
    entry.duration_minutes = int(duration)
    entry.save()

    return Response(CheckInSerializer(entry).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_referral(request):
    """
    GET /api/members/referral/ → {code, link, reward, joined, earned}: the member's referral link
    and how many people joined with it. Members only.
    """
    if request.user.role != 'member':
        return Response({'detail': 'Referral links are for members.'}, status=status.HTTP_403_FORBIDDEN)
    from .referrals import summary
    return Response(summary(request.user))
