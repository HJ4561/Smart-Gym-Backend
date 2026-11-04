"""
Admin → Nutrition: nutritionist profiles (shown to members), the plans members request
(starter targets from a template until staff review them) and body-scan (DEXA) results.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from apps.accounts.models import User
from apps.core.i18n import bi, with_ar
from apps.core.validators import email_error
from apps.notifications.service import notify
from apps.nutrition.models import DexaScan, MealLog, NutritionistProfile, NutritionPlan, WaterIntake

from .catalog_ops import _apply_trainer, _save_image
from .permissions import IsAdminUser

GOALS = dict(NutritionPlan.GOAL_CHOICES)
STATUSES = dict(NutritionPlan.STATUS_CHOICES)


def _f(v):
    return round(float(v), 2) if v is not None else None


# ============================================================
# NUTRITIONISTS (same editable fields as trainers)
# ============================================================
def _nutritionist_row(n):
    u = n.user
    name = u.full_name or u.email
    return {
        'id': n.id, 'user_id': u.id, 'user_name': name, 'user_email': u.email, 'phone': u.phone or '',
        'first_name': u.first_name, 'last_name': u.last_name,
        'initials': ''.join(p[0] for p in name.split()[:2]).upper() or '?',
        'title': n.title, 'credentials': n.credentials, 'bio': n.bio, 'photo_url': n.photo_url,
        **with_ar(n, 'title', 'bio', 'credentials'), 'specialties_ar': n.specialties_ar or [],
        'specialties': n.specialties or [], 'years_experience': n.years_experience,
        'hourly_rate': _f(n.hourly_rate), 'rating': _f(n.rating), 'total_reviews': n.total_reviews,
        'is_verified': n.is_verified, 'is_accepting_clients': n.is_accepting_clients, 'is_active': u.is_active,
        'active_plans': n.plans.filter(status='active').count(),
    }


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def nutritionists(request):
    """GET /api/admin/nutrition/nutritionists/?search= · POST {email, first_name, last_name, title, …}"""
    if request.method == 'GET':
        qs = NutritionistProfile.objects.select_related('user').order_by('-rating', 'user__first_name')
        q = (request.query_params.get('search') or '').strip()
        if q:
            qs = qs.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q)
                           | Q(user__email__icontains=q) | Q(title__icontains=q))
        return Response([_nutritionist_row(n) for n in qs])
    errors = {}
    email = str(request.data.get('email') or '').strip().lower()
    msg = email_error(email)
    if msg:
        errors['email'] = msg
    u = User.objects.filter(email__iexact=email).first() if email else None
    if u is not None and NutritionistProfile.objects.filter(user=u).exists():
        errors['email'] = 'This person is already a nutritionist.'
    if u is None:
        u = User(email=email, username=email, role='nutritionist')
        u.set_unusable_password()
    n = NutritionistProfile(user=u, hourly_rate=Decimal('0'))
    _apply_trainer(n, u, {'title': request.data.get('title', ''), 'first_name': request.data.get('first_name', ''),
                          **request.data}, errors)
    if errors:
        return Response(errors, status=400)
    with transaction.atomic():
        if u.role not in ('admin', 'manager'):
            u.role = 'nutritionist'
        u.save()
        n.user = u
        n.save()
    return Response(_nutritionist_row(n), status=201)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def nutritionist_update(request, nutritionist_id):
    """PATCH any field · DELETE when they have no plans (hide them otherwise)."""
    n = get_object_or_404(NutritionistProfile.objects.select_related('user'), id=nutritionist_id)
    if request.method == 'DELETE':
        plans = n.plans.count()
        if plans:
            return Response({'detail': f'{n.user.full_name} has {plans} nutrition plan{"s" if plans != 1 else ""} on '
                                       'record, so they can’t be deleted — hide them instead.'}, status=400)
        with transaction.atomic():
            user = n.user
            n.delete()
            if user.role == 'nutritionist':
                user.role = 'member'
                user.save(update_fields=['role'])
        return Response(status=204)
    errors = {}
    _apply_trainer(n, n.user, {k: v for k, v in request.data.items() if k != 'offers_1on1'}, errors)
    if errors:
        return Response(errors, status=400)
    with transaction.atomic():
        n.user.save()
        n.save()
    return Response(_nutritionist_row(n))


@api_view(['POST'])
@permission_classes([IsAdminUser])
@parser_classes([MultiPartParser, FormParser])
def nutritionist_photo(request, nutritionist_id):
    n = get_object_or_404(NutritionistProfile.objects.select_related('user'), id=nutritionist_id)
    url, err = _save_image(request, 'nutritionists')
    if err:
        return Response({'image': [err]}, status=400)
    n.photo_url = url
    n.save(update_fields=['photo_url', 'updated_at'])
    return Response(_nutritionist_row(n))


# ============================================================
# PLANS
# ============================================================
def _plan_row(p):
    return {
        'id': p.id, 'member_id': p.member_id, 'member': p.member.full_name or p.member.email, 'member_email': p.member.email,
        'nutritionist_id': p.nutritionist_id, 'nutritionist': p.nutritionist.user.full_name or p.nutritionist.user.email,
        'name': p.name, 'goal': p.goal, 'goal_label': GOALS.get(p.goal, p.goal), 'status': p.status,
        'target_calories': p.target_calories, 'target_protein_g': p.target_protein_g, 'target_carbs_g': p.target_carbs_g,
        'target_fats_g': p.target_fats_g, 'target_water_ml': p.target_water_ml, 'cycle_weeks': p.cycle_weeks,
        'notes': p.notes, 'reviewed_at': p.reviewed_at, 'created_at': p.created_at, 'price': _f(p.price),
    }


@api_view(['GET'])
@permission_classes([IsAdminUser])
def plans(request):
    """GET /api/admin/nutrition/plans/?status=to_review|active|all&q="""
    qs = NutritionPlan.objects.select_related('member', 'nutritionist__user').order_by('reviewed_at', '-created_at')
    if request.query_params.get('status') != 'pending_payment':
        qs = qs.exclude(status='pending_payment')   # unpaid requests aren't the nutritionist's work yet
    st = request.query_params.get('status', 'active')
    if st == 'to_review':
        qs = qs.filter(status='active', reviewed_at__isnull=True)
    elif st in STATUSES:
        qs = qs.filter(status=st)
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(member__email__icontains=q) | Q(member__first_name__icontains=q) | Q(member__last_name__icontains=q))
    return Response([_plan_row(p) for p in qs[:200]])


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def plan_update(request, plan_id):
    """PATCH targets / notes / status — marks the plan reviewed and tells the member."""
    p = get_object_or_404(NutritionPlan.objects.select_related('member', 'nutritionist__user'), id=plan_id)
    errors = {}
    limits = {'target_calories': (800, 8000), 'target_protein_g': (0, 600), 'target_carbs_g': (0, 1200),
              'target_fats_g': (0, 400), 'target_water_ml': (500, 10000), 'cycle_weeks': (1, 52)}
    for f, (lo, hi) in limits.items():
        if f in request.data:
            try:
                v = int(request.data[f])
                if not lo <= v <= hi:
                    raise ValueError
                setattr(p, f, v)
            except (TypeError, ValueError):
                errors[f] = f'Enter {lo}–{hi}.'
    if 'goal' in request.data:
        if request.data['goal'] not in GOALS:
            errors['goal'] = 'Choose a goal.'
        else:
            p.goal = request.data['goal']
    if 'status' in request.data:
        if request.data['status'] not in STATUSES:
            errors['status'] = 'Choose a status.'
        else:
            p.status = request.data['status']
    if 'notes' in request.data:
        p.notes = str(request.data.get('notes') or '')[:4000]
    if 'name' in request.data:
        p.name = str(request.data.get('name') or '').strip()[:120] or p.name
    if errors:
        return Response(errors, status=400)
    first_review = p.reviewed_at is None
    p.reviewed_at = timezone.now()
    p.save()
    who = p.nutritionist.user.full_name
    notify(p.member, 'nutrition',
           bi('Your nutrition plan was reviewed', 'تمت مراجعة خطة تغذيتك') if first_review
           else bi('Your nutrition plan was updated', 'تم تحديث خطة تغذيتك'),
           bi(f'{who or "Your nutritionist"} set your daily targets: {p.target_calories} kcal, {p.target_protein_g} g protein.',
              f'حدّد {who or "أخصائي التغذية"} أهدافك اليومية: {p.target_calories} سعرة حرارية، {p.target_protein_g} جم بروتين.'),
           icon='restaurant', action_label='Open my plan',
           action_path='/dashboard/nutrition-plan')
    return Response(_plan_row(p))


# ============================================================
# MEAL LOG (what the member logged in the portal)
# ============================================================
@api_view(['GET'])
@permission_classes([IsAdminUser])
def member_meals(request, member_id):
    """GET /api/admin/nutrition/members/<id>/meals/?days=14 — meals and water per day, newest first."""
    member = get_object_or_404(User, id=member_id)
    try:
        days = min(max(int(request.query_params.get('days', 14)), 1), 90)
    except ValueError:
        days = 14
    since = timezone.localdate() - timezone.timedelta(days=days - 1)
    water = {w.date: w.amount_ml for w in WaterIntake.objects.filter(member=member, date__gte=since)}
    out = {}
    for m in MealLog.objects.filter(member=member, logged_date__gte=since).order_by('-logged_at'):
        day = out.setdefault(m.logged_date, {'date': m.logged_date, 'meals': [], 'calories': 0, 'protein_g': 0.0,
                                             'carbs_g': 0.0, 'fats_g': 0.0})
        day['meals'].append({'id': m.id, 'meal_type': m.meal_type, 'meal_label': m.get_meal_type_display(),
                             'description': m.description, 'calories': m.calories, 'protein_g': _f(m.protein_g),
                             'carbs_g': _f(m.carbs_g), 'fats_g': _f(m.fats_g), 'logged_at': m.logged_at})
        day['calories'] += m.calories
        for k in ('protein_g', 'carbs_g', 'fats_g'):
            day[k] = round(day[k] + float(getattr(m, k) or 0), 1)
    for d, ml in water.items():
        out.setdefault(d, {'date': d, 'meals': [], 'calories': 0, 'protein_g': 0.0, 'carbs_g': 0.0, 'fats_g': 0.0})
    rows = sorted(out.values(), key=lambda r: r['date'], reverse=True)
    for r in rows:
        r['water_ml'] = water.get(r['date'], 0)
    return Response(rows)


# ============================================================
# BODY SCANS (DEXA)
# ============================================================
def _scan_row(s):
    return {'id': s.id, 'scan_date': s.scan_date, 'weight_kg': _f(s.weight_kg), 'body_fat_percent': _f(s.body_fat_percent),
            'lean_mass_kg': _f(s.lean_mass_kg), 'fat_mass_kg': _f(s.fat_mass_kg),
            'visceral_fat_kg': _f(s.visceral_fat_kg), 'notes': s.notes}


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def member_scans(request, member_id):
    """GET/POST /api/admin/nutrition/members/<id>/scans/ — record a member's scan result."""
    member = get_object_or_404(User, id=member_id, role='member')
    if request.method == 'GET':
        return Response([_scan_row(s) for s in member.dexa_scans.order_by('-scan_date', '-id')])
    errors, vals = {}, {}
    for f, lo, hi, req in (('weight_kg', 20, 400, True), ('body_fat_percent', 2, 75, True), ('lean_mass_kg', 10, 200, True),
                           ('fat_mass_kg', 1, 250, True), ('visceral_fat_kg', 0, 20, False)):
        v = request.data.get(f)
        if v in (None, ''):
            if req:
                errors[f] = 'Required.'
            vals[f] = None
            continue
        try:
            d = Decimal(str(v)).quantize(Decimal('0.01'))
            if not Decimal(lo) <= d <= Decimal(hi):
                raise InvalidOperation
            vals[f] = d
        except (InvalidOperation, ValueError):
            errors[f] = f'Enter {lo}–{hi}.'
    if errors:
        return Response(errors, status=400)
    s = DexaScan.objects.create(member=member, notes=str(request.data.get('notes') or '')[:2000], **vals)
    notify(member, 'nutrition', bi('New body scan result', 'نتيجة فحص جسم جديدة'),
           bi(f'Body fat {s.body_fat_percent}% · lean mass {s.lean_mass_kg} kg.',
              f'نسبة الدهون {s.body_fat_percent}% · الكتلة العضلية {s.lean_mass_kg} كجم.'),
           icon='monitor_weight', action_label='See results', action_path='/dashboard/nutritionists')
    return Response(_scan_row(s), status=201)
