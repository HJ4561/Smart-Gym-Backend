from datetime import date, timedelta
from django.utils import timezone
from django.db.models import Q, Count, F
from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import (
    api_view, permission_classes, parser_classes, authentication_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.response import Response

from apps.members.models import (
    MembershipPlan, MembershipRegistration, Membership, CheckIn, FaceCheck,
)
from apps.members.serializers import (
    MembershipPlanSerializer, MembershipSerializer, CheckInSerializer,
)
from apps.members.services import (
    approve_registration, reject_registration, bulk_approve_registrations,
)
from apps.catalog.models import Course, TrainerProfile
from apps.catalog.serializers import (
    CourseListSerializer, TrainerProfileSerializer,
)

from .serializers import (
    PublicPlanSerializer,
    RegistrationCreateSerializer,
    RegistrationStatusSerializer,
    AdminRegistrationSerializer,
    BulkApproveSerializer,
    PaymentIdSerializer,
    RejectSerializer,
)
from .permissions import IsAdminOrManager
from . import moyasar as moyasar_api
from .moyasar import (
    checkout_params, sync_payment, webhook_token_valid,
    MoyasarError, MoyasarUnavailable,
)
import logging

logger = logging.getLogger(__name__)

# ============================================================
# PUBLIC — CATALOG
# ============================================================
@api_view(['GET'])
@permission_classes([AllowAny])
def public_plans(request):
    """GET /api/public/plans/"""
    qs = MembershipPlan.objects.filter(is_active=True)
    return Response(PublicPlanSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def public_courses(request):
    """GET /api/public/courses/?category=&featured=true"""
    qs = Course.objects.filter(status='published').select_related('trainer__user')
    if request.query_params.get('featured') == 'true':
        qs = qs.filter(is_featured=True)
    if (cat := request.query_params.get('category')):
        qs = qs.filter(category__iexact=cat)
    return Response(CourseListSerializer(qs, many=True).data)


REFERRAL_LOOKUPS_PER_IP_HOUR = 60


@api_view(['GET'])
@permission_classes([AllowAny])
def public_referral(request):
    """
    GET /api/public/referral/?code=ABCD2345 → is this referral code valid, and whose is it (first name only)?
    The signup page calls it when it opens with ?ref=… so it can say "Invited by Sara". 404 when not valid.
    """
    from datetime import timedelta
    from apps.accounts import ratelimit
    from apps.members import referrals
    ip = ratelimit.client_ip(request)
    if ratelimit.count('referral_ip', ip, timedelta(hours=1)) >= REFERRAL_LOOKUPS_PER_IP_HOUR:
        return Response({'detail': 'Too many tries. Please try again in an hour.'},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    ratelimit.hit('referral_ip', ip)
    code = referrals.normalize(request.query_params.get('code'))
    referrer = referrals.referrer_for(code)
    if referrer is None:
        return Response({'detail': 'This referral code isn’t valid. Check it, or leave it empty.', 'valid': False},
                        status=status.HTTP_404_NOT_FOUND)
    return Response({'valid': True, 'code': code, 'referrer_first_name': referrer.first_name or ''})


@api_view(['GET'])
@permission_classes([AllowAny])
def public_trainers(request):
    """GET /api/public/trainers/?verified=true"""
    qs = TrainerProfile.objects.select_related('user').filter(user__is_active=True)   # hidden by staff → not listed
    if request.query_params.get('verified') == 'true':
        qs = qs.filter(is_verified=True)
    return Response(TrainerProfileSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def public_facilities(request):
    """
    GET /api/public/facilities/
    Reads from apps.facilities if it exposes a Facility model;
    otherwise returns a static list.
    """
    try:
        from apps.facilities.models import Facility
        qs = Facility.objects.filter(is_active=True) if hasattr(Facility, 'is_active') \
             else Facility.objects.all()
        data = [
            {
                'id': f.id,
                'name': getattr(f, 'name', str(f)),
                'name_ar': getattr(f, 'name_ar', ''),
                'description': getattr(f, 'description', ''),
                'description_ar': getattr(f, 'description_ar', ''),
                'capacity': getattr(f, 'capacity', None),
                'image_url': getattr(getattr(f, 'image', None), 'url', None),
            } for f in qs
        ]
        return Response(data)
    except Exception:
        # Fallback static list — public site can render this immediately
        return Response([
            {'id': 1, 'name': 'Olympic Swimming Pool', 'description': '8-lane racing reservoir.', 'capacity': 40},
            {'id': 2, 'name': 'Pro Basketball Court', 'description': 'FIBA hardwood.', 'capacity': 20},
            {'id': 3, 'name': 'Combat & Boxing Pit', 'description': 'Octagon + force-sensor bags.', 'capacity': 30},
            {'id': 4, 'name': 'Steam & Cryo Recovery', 'description': 'Cryo chambers + sauna suites.', 'capacity': 15},
            {'id': 5, 'name': 'Volleyball Arena', 'description': 'League-grade flooring.', 'capacity': 24},
            {'id': 6, 'name': 'Heavy Iron Arena', 'description': '18 competition power racks.', 'capacity': 60},
        ])


@api_view(['GET'])
@permission_classes([AllowAny])
def public_articles(request):
    """GET /api/public/articles/  — reads from apps.cms if available."""
    try:
        from apps.cms.models import Article
        qs = Article.objects.filter(is_published=True)[:30] if hasattr(Article, 'is_published') \
             else Article.objects.all()[:30]
        data = [
            {
                'id': a.id,
                'title': getattr(a, 'title', ''),
                'title_ar': getattr(a, 'title_ar', ''),
                'slug': getattr(a, 'slug', ''),
                'excerpt': getattr(a, 'excerpt', ''),
                'excerpt_ar': getattr(a, 'excerpt_ar', ''),
                'cover_image_url': getattr(a, 'cover_image_url', '') or
                                   getattr(getattr(a, 'cover_image', None), 'url', ''),
                'published_at': getattr(a, 'published_at', None) or getattr(a, 'created_at', None),
                'author': getattr(getattr(a, 'author', None), 'full_name', ''),
            } for a in qs
        ]
        return Response(data)
    except Exception:
        return Response([])


# ============================================================
# PUBLIC — REGISTRATION
# ============================================================
SIGNUPS_PER_IP_HOUR = 10
PHOTO_CHECKS_PER_IP_HOUR = 60


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def public_liveness_start(request):
    """POST /api/public/liveness/start/ → {token, stage, prompt}: begins the live face check."""
    from apps.accounts import ratelimit
    from apps.members import liveness
    try:
        token, step = liveness.start(ratelimit.client_ip(request))
    except liveness.LivenessError as e:
        return Response({'detail': str(e), 'code': e.code}, status=e.status)
    return Response({'token': token, **step})


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
@parser_classes([MultiPartParser, FormParser])
def public_liveness_frame(request):
    """
    POST /api/public/liveness/frame/  multipart: token, frame (small JPEG)
    → {stage: straight|turn1|turn2|done, prompt, face, hint}
    """
    from apps.members import liveness
    f = request.FILES.get('frame')
    if not f or f.size > 600_000:
        return Response({'detail': 'Send one small camera frame.', 'code': 'frame'}, status=400)
    try:
        return Response(liveness.frame(request.data.get('token'), f))
    except liveness.LivenessError as e:
        return Response({'detail': str(e), 'code': e.code}, status=e.status)


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
@parser_classes([MultiPartParser, FormParser])
def public_photo_check(request):
    """
    POST /api/public/photo-check/   multipart: photo (file), liveness_token
    The signup camera asks right after a capture, so the person hears "no face found" at once
    instead of after filling in the whole form. With a passed live face check (liveness_token)
    the photo is registered as the one this person may sign up with. Registration checks again.
    200 {"ok": true, "live": bool} · 400 {"photo": [message], "code": "no_face" | "blurry" | "not_live" | …}
    """
    from apps.accounts import ratelimit
    from apps.core.photo_quality import photo_check
    from .serializers import MAX_PHOTO_BYTES

    ip = ratelimit.client_ip(request)
    if ratelimit.count('photo_check_ip', ip, timedelta(hours=1)) >= PHOTO_CHECKS_PER_IP_HOUR:
        return Response({'detail': 'Too many photo checks from this connection. Try again in an hour.'},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    ratelimit.hit('photo_check_ip', ip)
    photo = request.FILES.get('photo')
    if not photo:
        return Response({'photo': ['Take a photo with the camera.'], 'code': 'missing'},
                        status=status.HTTP_400_BAD_REQUEST)
    if photo.size > MAX_PHOTO_BYTES:
        return Response({'photo': ['Photo must be under 4 MB.'], 'code': 'too_large'},
                        status=status.HTTP_400_BAD_REQUEST)
    found = photo_check(photo)
    if found:
        code, message = found
        return Response({'photo': [message], 'code': code},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE if code == 'unavailable'
                        else status.HTTP_400_BAD_REQUEST)
    if request.data.get('liveness_token'):
        from apps.members import liveness
        try:
            liveness.attach_photo(request.data['liveness_token'], photo)
        except liveness.LivenessError as e:
            return Response({'photo': [str(e)], 'code': e.code}, status=e.status)
        return Response({'ok': True, 'live': True})
    return Response({'ok': True, 'live': False})


@api_view(['POST'])
@permission_classes([AllowAny])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def public_register(request):
    """
    POST /api/public/register/
    multipart/form-data with:
      iqama_number, full_name, email, phone, address, age, gender,
      photo (file), plan (id), duration_months, password

    Returns the registration status plus `payment` — the parameters the
    frontend passes to the Moyasar form (amount is computed server-side).
    """
    from datetime import timedelta

    from apps.accounts import ratelimit
    ip = ratelimit.client_ip(request)
    if ratelimit.count('signup_ip', ip, timedelta(hours=1)) >= SIGNUPS_PER_IP_HOUR:
        return Response({'detail': 'Too many signups from this connection. Try again in an hour.'},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    serializer = RegistrationCreateSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    # The site's language: an explicit "language" field, else the request's Accept-Language.
    from apps.core.i18n import language
    wanted = str(request.data.get('language') or '').lower()
    from apps.members import referrals
    code = referrals.normalize(request.data.get('referral_code'))
    if code and referrals.referrer_for(code) is None:
        return Response({'referral_code': ['This referral code isn’t valid. Check it, or leave it empty.']},
                        status=status.HTTP_400_BAD_REQUEST)
    reg = serializer.save(language=wanted if wanted in ('ar', 'en') else language(), referral_code=code)
    ratelimit.hit('signup_ip', ip)
    return Response(
        {**RegistrationStatusSerializer(reg).data, 'amount': str(reg.payment_amount),
         'facilities': reg.facilities or [], 'payment': checkout_params(reg)},
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def public_register_status(request, reg_id):
    """GET /api/public/register/<id>/status/"""
    reg = get_object_or_404(MembershipRegistration, id=reg_id)
    return Response(RegistrationStatusSerializer(reg).data)


# ============================================================
# PUBLIC — PAYMENT (Moyasar)
# ============================================================
def _sync_response(payment_id, registration_id=None):
    try:
        reg = sync_payment(payment_id, registration_id=registration_id)
    except MoyasarUnavailable as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except MoyasarError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(RegistrationStatusSerializer(reg).data)


@api_view(['POST'])
@permission_classes([AllowAny])
def public_payment_attach(request, reg_id):
    """
    POST /api/public/register/<id>/payment/attach/   { "payment_id": "..." }
    Called from the Moyasar form's on_completed hook, before any 3-D Secure
    redirect, so the payment is recorded even if the user never returns.
    """
    get_object_or_404(MembershipRegistration, id=reg_id)
    ser = PaymentIdSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    return _sync_response(ser.validated_data['payment_id'], registration_id=reg_id)


@api_view(['POST'])
@permission_classes([AllowAny])
def public_payment_verify(request):
    """
    POST /api/public/payments/moyasar/verify/   { "payment_id": "..." }
    Called by the public site's callback page with the ?id= Moyasar appends.
    """
    ser = PaymentIdSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    return _sync_response(ser.validated_data['payment_id'])


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def moyasar_webhook(request):
    """
    POST /api/public/payments/moyasar/webhook/
    Configured in the Moyasar dashboard. The body is only used for its
    secret_token and payment id — the payment itself is re-fetched.
    """
    if not webhook_token_valid(request.data.get('secret_token')):
        logger.warning('Moyasar webhook rejected: bad secret_token')
        return Response({'detail': 'Invalid token.'}, status=status.HTTP_403_FORBIDDEN)

    payment_id = (request.data.get('data') or {}).get('id')
    if not payment_id:
        return Response({'detail': 'No payment id.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Member payments (top-ups, orders, sessions, courses, renewals) carry an
        # intent_id; signup payments carry a registration_id.
        payment = moyasar_api.fetch_payment(payment_id)
        if (payment.get('metadata') or {}).get('intent_id'):
            from apps.payments.gateway import sync_intent
            sync_intent(payment_id, payment=payment)
        else:
            sync_payment(payment_id, payment=payment)
    except MoyasarUnavailable:
        # Temporary failure — non-2xx so Moyasar retries (guide §7).
        return Response({'detail': 'Retry later.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except MoyasarError as exc:
        # Not ours / not linked — acknowledge so Moyasar stops retrying.
        logger.warning('Moyasar webhook %s (%s) ignored: %s', payment_id, request.data.get('type'), exc)
    return Response({'ok': True})


# ============================================================
# ADMIN — REGISTRATIONS
# ============================================================
@api_view(['GET'])
@permission_classes([IsAdminOrManager])
def admin_registrations(request):
    """
    GET /api/admin/registrations/?status=pending&search=&today=true
        &sort=submitted_at|name|amount|status (−desc)  &filter_status= &filter_payment_status= &filter_plan=
        &filter_submitted_after= &filter_submitted_before=
    """
    from apps.core import listing
    qs = listing.apply(
        MembershipRegistration.objects.select_related('plan', 'approved_by', 'created_user__membership'),
        request.query_params, default_sort='-submitted_at',
        sorts={'submitted_at': 'submitted_at', 'name': 'full_name', 'amount': 'payment_amount', 'status': 'status'},
        filters={'status': ('status', 'str'), 'payment_status': ('payment_status', 'str'), 'plan': ('plan__name', 'str'),
                 'submitted_after': ('submitted_at__date__gte', 'date'),
                 'submitted_before': ('submitted_at__date__lte', 'date')})

    st = request.query_params.get('status')
    if st and st != 'all':
        qs = qs.filter(status=st)

    if request.query_params.get('today') == 'true':
        qs = qs.filter(submitted_at__date=date.today())

    search = request.query_params.get('search', '').strip()
    if search:
        qs = qs.filter(
            Q(iqama_number__icontains=search) |
            Q(full_name__icontains=search) |
            Q(email__icontains=search) |
            Q(phone__icontains=search)
        )

    serializer = AdminRegistrationSerializer(qs, many=True, context={'request': request})
    return Response({
        'count': qs.count(),
        'results': serializer.data,
    })


@api_view(['GET'])
@permission_classes([IsAdminOrManager])
def admin_registration_detail(request, reg_id):
    """GET /api/admin/registrations/<id>/"""
    reg = get_object_or_404(
        MembershipRegistration.objects.select_related('plan', 'approved_by', 'created_user__membership'),
        id=reg_id,
    )
    return Response(AdminRegistrationSerializer(reg, context={'request': request}).data)


@api_view(['POST'])
@permission_classes([IsAdminOrManager])
def admin_approve_registration(request, reg_id):
    """POST /api/admin/registrations/<id>/approve/"""
    reg = get_object_or_404(MembershipRegistration, id=reg_id)
    try:
        user = approve_registration(reg, approved_by=request.user)
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    membership = Membership.objects.get(member=user)
    return Response({
        'ok': True,
        'registration': AdminRegistrationSerializer(reg, context={'request': request}).data,
        'user': {'id': user.id, 'email': user.email, 'full_name': user.full_name},
        'membership': MembershipSerializer(membership).data,
    })


@api_view(['POST'])
@permission_classes([IsAdminOrManager])
def admin_reject_registration(request, reg_id):
    """POST /api/admin/registrations/<id>/reject/  { reason, refund }"""
    reg = get_object_or_404(MembershipRegistration, id=reg_id)
    ser = RejectSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    try:
        refunded = reject_registration(reg, reason=ser.validated_data['reason'], rejected_by=request.user,
                                       refund=ser.validated_data['refund'])
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({'ok': True, 'status': reg.status, 'refunded': refunded})


@api_view(['POST'])
@permission_classes([IsAdminOrManager])
def admin_bulk_approve(request):
    """
    POST /api/admin/registrations/bulk-approve/
    Body: { "ids": [1,2,3] }              → approve only these
          { "only_today": true }          → approve all paid submitted today
          {}                              → approve ALL paid (pending admin approval)
    Unpaid registrations are never approved.
    """
    ser = BulkApproveSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    ids = ser.validated_data.get('ids')
    only_today = ser.validated_data.get('only_today', False)

    qs = MembershipRegistration.objects.filter(status=MembershipRegistration.STATUS_PAID)
    if ids:
        qs = qs.filter(id__in=ids)
    if only_today:
        qs = qs.filter(submitted_at__date=date.today())

    approved, failed = bulk_approve_registrations(qs, approved_by=request.user)
    return Response({
        'ok': True,
        'approved_count': approved,
        'failed_count': len(failed),
        'failed': [{'id': rid, 'error': err} for rid, err in failed],
    })


@api_view(['GET'])
@permission_classes([IsAdminOrManager])
def admin_registration_stats(request):
    """GET /api/admin/registrations/stats/"""
    today = date.today()
    qs = MembershipRegistration.objects
    return Response({
        'pending': qs.filter(status=MembershipRegistration.STATUS_PENDING).count(),
        'paid': qs.filter(status=MembershipRegistration.STATUS_PAID).count(),
        'approved': qs.filter(status=MembershipRegistration.STATUS_APPROVED).count(),
        'rejected': qs.filter(status=MembershipRegistration.STATUS_REJECTED).count(),
        'today_total': qs.filter(submitted_at__date=today).count(),
        'today_pending': qs.filter(
            submitted_at__date=today,
            status__in=[
                MembershipRegistration.STATUS_PENDING,
                MembershipRegistration.STATUS_PAID,
            ],
        ).count(),
    })


# ============================================================
# ADMIN — MEMBER LOOKUP (gym entry)
# ============================================================
@api_view(['GET'])
@permission_classes([IsAdminOrManager])
def admin_member_lookup(request):
    """
    GET /api/admin/members/lookup/?iqama=XXX   (typed Iqama number)
    GET /api/admin/members/lookup/?pass=SGPASS:...   (member's digital pass QR, scanned)
    Returns member + membership + registration photo + remaining days + today's session.
    """
    iqama = request.query_params.get('iqama', '').strip()
    pass_token = request.query_params.get('pass', '').strip()
    if pass_token:
        from apps.members.passes import InvalidPass, read_token
        try:
            membership = read_token(pass_token)
        except InvalidPass as exc:
            return Response({'found': False, 'reason': 'invalid_pass', 'detail': str(exc)}, status=400)
    elif not iqama:
        return Response({'detail': 'iqama or pass query param required.'}, status=400)
    else:
        # Iqama isn't unique on memberships: when two share one, the one that lets them in wins,
        # then the latest expiry.
        candidates = list(Membership.objects.select_related('member', 'plan')
                          .filter(iqama_number__iexact=iqama).order_by('-expiry_date', '-id'))
        membership = next((m for m in candidates if m.allows_entry), candidates[0] if candidates else None)
        if membership is None:
            # Maybe they registered but aren't approved yet
            pending = MembershipRegistration.objects.filter(iqama_number__iexact=iqama).first()
            if pending:
                return Response({
                    'found': False,
                    'reason': 'registration_pending',
                    'registration': AdminRegistrationSerializer(pending, context={'request': request}).data,
                }, status=status.HTTP_404_NOT_FOUND)
            return Response({'found': False, 'reason': 'not_found'}, status=404)

    member = membership.member

    # Registration photo (source)
    reg = getattr(member, 'source_registration', None)
    reg_photo_url = None
    if reg and reg.photo:
        reg_photo_url = request.build_absolute_uri(reg.photo.url)

    # Today's active session?
    today = timezone.localdate()
    active = CheckIn.objects.filter(
        member=member,
        check_in_time__date=today,
        check_out_time__isnull=True,
    ).first()

    return Response({
        'found': True,
        'member': {
            'id': member.id,
            'email': member.email,
            'full_name': member.full_name,
            'phone': member.phone,
            'avatar_url': request.build_absolute_uri(member.avatar.url) if member.avatar else None,
        },
        'membership': MembershipSerializer(membership).data,
        'registration_photo_url': reg_photo_url,
        'is_valid': membership.allows_entry,
        'active_check_in': CheckInSerializer(active).data if active else None,
        'facilities': [{'name': f.facility_name, 'expires_on': f.expires_on.isoformat()}
                       for f in member.facility_access.filter(expires_on__gt=today)],
    })


@api_view(['POST'])
@permission_classes([IsAdminOrManager])
def admin_member_face_verify(request, member_id):
    """
    POST /api/admin/members/<id>/face-verify/
    Body: { "match": true|false, "confidence": 0.877, "threshold": 0.363, "source": "gate" }
    Records the comparison (granted or denied) as a FaceCheck — the security audit trail.
    The matching itself happens at the gate (gym_entry_system/gate_check.py).
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    member = get_object_or_404(User, id=member_id)

    try:
        confidence = float(request.data.get('confidence'))
        threshold = request.data.get('threshold')
        threshold = float(threshold) if threshold is not None else None
    except (TypeError, ValueError):
        return Response({'detail': 'confidence (and threshold, if given) must be numbers.'},
                        status=status.HTTP_400_BAD_REQUEST)
    matched = request.data.get('match')
    if not isinstance(matched, bool):
        return Response({'detail': 'match must be true or false.'}, status=status.HTTP_400_BAD_REQUEST)

    membership = Membership.objects.filter(member=member).only('iqama_number').first()
    fc = FaceCheck.objects.create(
        member=member,
        iqama_number=membership.iqama_number if membership else '',
        matched=matched,
        confidence=confidence,
        threshold=threshold,
        verified_by=request.user,
        source=str(request.data.get('source') or 'gate')[:30],
    )
    if not matched:
        logger.warning('Face check DENIED: member #%s (%s) confidence %.3f by %s',
                       member.id, fc.iqama_number, confidence, request.user.email)

    return Response({
        'ok': True,
        'face_check_id': fc.id,
        'member_id': member.id,
        'matched': matched,
        'confidence': confidence,
        'verified_by': request.user.email,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAdminOrManager])
def admin_check_in(request, member_id):
    """
    POST /api/admin/members/<id>/check-in/
    Body: { "facility": "...", "zone": "...", "face_verified": true, "face_check_id": 12 }
    face_check_id (optional) links the check-in to the matching FaceCheck; it must be
    a successful check for this member that isn't linked to another check-in.

    Refused unless the member's current membership is valid. If the plan has a
    session_limit_minutes, the response carries the session timer
    (session_limit_minutes + expected_checkout_time).
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    member = get_object_or_404(User, id=member_id)

    # Plan validation — same rule as the gate lookup's is_valid (Membership.allows_entry).
    membership = Membership.objects.filter(member=member).select_related('plan').first()  # newest
    if membership is None:
        return Response({'detail': 'Member has no membership.'}, status=status.HTTP_400_BAD_REQUEST)
    if not membership.allows_entry:
        return Response({
            'detail': 'Membership is not valid for entry.',
            'status': membership.status,
            'is_frozen': membership.is_frozen,
            'days_remaining': membership.days_remaining,
        }, status=status.HTTP_400_BAD_REQUEST)

    # Prevent double check-in
    today = timezone.localdate()
    existing = CheckIn.objects.filter(
        member=member,
        check_in_time__date=today,
        check_out_time__isnull=True,
    ).first()
    if existing:
        return Response({
            'detail': 'Already checked in — check out first.',
            'check_in_id': existing.id,
        }, status=status.HTTP_400_BAD_REQUEST)

    face_check = None
    if request.data.get('face_check_id') is not None:
        face_check = FaceCheck.objects.filter(
            id=request.data.get('face_check_id'), member=member,
            matched=True, check_in__isnull=True,
        ).first()
        if face_check is None:
            return Response({'detail': 'face_check_id is not an unused successful face check for this member.'},
                            status=status.HTTP_400_BAD_REQUEST)

    limit = membership.plan.session_limit_minutes
    ci = CheckIn.objects.create(
        member=member,
        facility=request.data.get('facility', 'Metro Station Core 01'),
        zone=request.data.get('zone', ''),
        verified_by=request.user,
        face_verified=bool(request.data.get('face_verified', False)) or face_check is not None,
        session_limit_minutes=limit,
    )
    if limit:
        # check_in_time is set by auto_now_add on create, so compute afterwards.
        ci.expected_checkout_time = ci.check_in_time + timedelta(minutes=limit)
        ci.save(update_fields=['expected_checkout_time'])
    if face_check is not None:
        face_check.check_in = ci
        face_check.save(update_fields=['check_in'])

    # Increment the visit counter (atomic, avoids lost updates on concurrent check-ins).
    Membership.objects.filter(id=membership.id).update(total_check_ins=F('total_check_ins') + 1)

    # Streak milestones + a notification for the member.
    from apps.attendance.views import _update_streak_milestones
    from apps.notifications.service import notify
    _update_streak_milestones(member)
    from apps.core.i18n import bi
    at = timezone.localtime(ci.check_in_time)
    notify(member, 'attendance', bi('Checked in', 'تم تسجيل الدخول'),
           bi(f'Welcome in! Checked in at {at:%H:%M}.' + (f' Your session limit is {limit} minutes.' if limit else ''),
              f'أهلًا بك! سُجّل دخولك الساعة {at:%H:%M}.' + (f' حد جلستك {limit} دقيقة.' if limit else '')),
           icon='login', action_label='Attendance', action_path='/dashboard/attendance')

    return Response(CheckInSerializer(ci).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAdminOrManager])
def admin_check_out(request, check_in_id):
    """
    POST /api/admin/check-ins/<check_in_id>/check-out/
    """
    ci = get_object_or_404(CheckIn, id=check_in_id)
    if ci.check_out_time:
        return Response({'detail': 'Already checked out.'}, status=400)

    ci.check_out_time = timezone.now()
    ci.duration_minutes = int((ci.check_out_time - ci.check_in_time).total_seconds() / 60)
    ci.save()
    return Response(CheckInSerializer(ci).data)