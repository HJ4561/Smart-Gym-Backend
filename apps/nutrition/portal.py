"""
The nutritionist portal (/api/nutritionist/…). A nutritionist works on their own clients directly —
plans, targets, notes, body scans go straight to the member. Their public profile (what members read
when choosing a nutritionist) waits for an admin, like every other staff upload (apps.approvals).

  GET   me/                          profile, stats and any profile change waiting for approval
  PATCH me/                          accepting new clients: at once · profile fields: sent for approval
  POST  me/photo/                    new photo, sent for approval
  GET   plans/?status=&q=&goal=      their clients' plans
  PATCH plans/<id>/                  targets, notes, status — the member is told
  GET   clients/<member_id>/         one client: details, plans with them, scans, last 14 days of meals
  GET   clients/<member_id>/meals/?days=
  GET/POST clients/<member_id>/scans/
"""
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.accounts.models import User
from apps.approvals.registry import pending_for, submit_update
from apps.admin_api import nutrition_ops as ops
from apps.admin_api.catalog_ops import _save_image

from .models import NutritionistProfile, NutritionPlan

# What members see on the nutritionist's card; changes wait for an admin.
PROFILE_FIELDS = ('title', 'title_ar', 'credentials', 'credentials_ar', 'bio', 'bio_ar', 'specialties',
                  'specialties_ar', 'years_experience', 'hourly_rate', 'photo_url')
SENT = 'Sent to an admin for approval. Members keep seeing the current version until then.'


class IsNutritionistWithProfile(BasePermission):
    message = 'This portal is for nutritionists.'

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == 'nutritionist'
                    and NutritionistProfile.objects.filter(user=u).exists())


def _me(request):
    return NutritionistProfile.objects.select_related('user').get(user=request.user)


def _my_client(request, member_id):
    """A member who has (or had) a plan with this nutritionist — nobody else's data."""
    me = _me(request)
    member = get_object_or_404(User, id=member_id, role='member')
    if not NutritionPlan.objects.filter(member=member, nutritionist=me).exists():
        from django.http import Http404
        raise Http404
    return me, member


def _profile(n):
    row = ops._nutritionist_row(n)
    waiting = pending_for('nutrition.profile', n.pk).filter(kind='update').first()
    row['pending_change'] = waiting.payload if waiting else None
    return row


@api_view(['GET', 'PATCH'])
@permission_classes([IsNutritionistWithProfile])
@parser_classes([JSONParser])
def me(request):
    n = _me(request)
    if request.method == 'GET':
        plans = n.plans.exclude(status='pending_payment')
        return Response({
            'profile': _profile(n),
            'stats': {
                'active_clients': plans.filter(status='active').values('member').distinct().count(),
                'to_review': plans.filter(status='active', reviewed_at__isnull=True).count(),
                'paused': plans.filter(status='paused').count(),
                'completed': plans.filter(status='completed').count(),
                'awaiting_payment': n.plans.filter(status='pending_payment').count(),
            },
        })
    data = request.data
    if 'is_accepting_clients' in data:            # day-to-day: straight away
        n.is_accepting_clients = bool(data['is_accepting_clients'])
        n.save(update_fields=['is_accepting_clients', 'updated_at'])
    profile = {k: v for k, v in data.items() if k in PROFILE_FIELDS}
    if not profile:
        return Response(_profile(n))
    cr, errors = submit_update(request.user, 'nutrition.profile', n, profile)
    if errors:
        return Response(errors, status=400)
    body = _profile(n)
    return Response({**body, 'detail': SENT} if cr else body, status=202 if cr else 200)


@api_view(['POST'])
@permission_classes([IsNutritionistWithProfile])
@parser_classes([MultiPartParser, FormParser])
def photo(request):
    n = _me(request)
    url, err = _save_image(request, 'nutritionists')
    if err:
        return Response({'image': [err]}, status=400)
    cr, errors = submit_update(request.user, 'nutrition.profile', n, {'photo_url': url})
    if errors:
        return Response(errors, status=400)
    return Response({**_profile(n), 'detail': SENT}, status=202)


@api_view(['GET'])
@permission_classes([IsNutritionistWithProfile])
def plans(request):
    qs = (NutritionPlan.objects.filter(nutritionist=_me(request)).select_related('member', 'nutritionist__user')
          .order_by('reviewed_at', '-created_at'))
    return Response([ops._plan_row(p) for p in ops.filter_plans(qs, request.query_params)[:300]])


@api_view(['PATCH'])
@permission_classes([IsNutritionistWithProfile])
def plan_detail(request, plan_id):
    p = get_object_or_404(NutritionPlan.objects.select_related('member', 'nutritionist__user'),
                          id=plan_id, nutritionist=_me(request))
    if p.status == 'pending_payment':
        return Response({'detail': 'This plan starts once the member pays the consultation fee.'}, status=400)
    return ops.update_plan(p, request.data)


@api_view(['GET'])
@permission_classes([IsNutritionistWithProfile])
def client(request, member_id):
    """Everything about one client for the nutritionist: who they are, their plans, scans and recent eating."""
    me, member = _my_client(request, member_id)
    days = ops.meal_days(member, 14)
    plan = NutritionPlan.objects.filter(member=member, nutritionist=me, status='active').first()
    logged = [d for d in days if d['meals']]
    avg = (lambda k: round(sum(d[k] for d in logged) / len(logged), 1) if logged else None)
    m = getattr(member, 'membership', None)
    return Response({
        'member': {'id': member.id, 'name': member.full_name or member.email, 'email': member.email,
                   'phone': member.phone or '', 'joined': member.date_joined,
                   'plan': m.plan.display_name if m else None},
        'plans': [ops._plan_row(p) for p in NutritionPlan.objects.filter(member=member, nutritionist=me)
                  .select_related('member', 'nutritionist__user').order_by('-created_at')],
        'scans': [ops._scan_row(s) for s in member.dexa_scans.order_by('-scan_date', '-id')[:20]],
        'last_14_days': {
            'days_logged': len(logged),
            'avg_calories': avg('calories'), 'avg_protein_g': avg('protein_g'),
            'avg_carbs_g': avg('carbs_g'), 'avg_fats_g': avg('fats_g'),
            'avg_water_ml': round(sum(d['water_ml'] for d in days) / len(days)) if days else None,
            'target_calories': plan.target_calories if plan else None,
            'target_protein_g': plan.target_protein_g if plan else None,
        },
        'days': days,
    })


@api_view(['GET'])
@permission_classes([IsNutritionistWithProfile])
def client_meals(request, member_id):
    _, member = _my_client(request, member_id)
    return Response(ops.meal_days(member, request.query_params.get('days', 14)))


@api_view(['GET', 'POST'])
@permission_classes([IsNutritionistWithProfile])
def client_scans(request, member_id):
    _, member = _my_client(request, member_id)
    if request.method == 'GET':
        return Response([ops._scan_row(s) for s in member.dexa_scans.order_by('-scan_date', '-id')])
    return ops.add_scan(member, request.data)


@api_view(['GET'])
@permission_classes([IsNutritionistWithProfile])
def clients(request):
    """GET /api/nutritionist/clients/?q= — one row per client with their newest plan and last meal log."""
    me = _me(request)
    qs = (User.objects.filter(nutrition_plans__nutritionist=me).exclude(nutrition_plans__status='pending_payment')
          .distinct())
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    since = timezone.localdate() - timezone.timedelta(days=13)
    qs = qs.annotate(meals_14d=Count('meal_logs', filter=Q(meal_logs__logged_date__gte=since), distinct=True),
                     scans=Count('dexa_scans', distinct=True))
    rows = []
    for u in qs.order_by('first_name', 'email')[:300]:
        plan = (NutritionPlan.objects.filter(member=u, nutritionist=me).exclude(status='pending_payment')
                .order_by('-created_at').first())
        rows.append({'id': u.id, 'name': u.full_name or u.email, 'email': u.email, 'meals_14d': u.meals_14d,
                     'scans': u.scans, 'plan': ops._plan_row(plan) if plan else None})
    st = request.query_params.get('status')
    if st:
        rows = [r for r in rows if r['plan'] and (r['plan']['status'] == st or
                                                  (st == 'to_review' and r['plan']['status'] == 'active'
                                                   and not r['plan']['reviewed_at']))]
    return Response(rows)

