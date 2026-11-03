from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    NutritionistProfile, NutritionPlan,
    MealLog, WaterIntake, DexaScan,
)
from .serializers import (
    NutritionistProfileSerializer,
    NutritionPlanSerializer,
    MealLogSerializer,
    WaterIntakeSerializer,
    DexaScanSerializer,
)


# ============================================
# NUTRITIONISTS (public directory)
# ============================================
@api_view(['GET'])
@permission_classes([AllowAny])
def nutritionists_list(request):
    """
    GET /api/nutrition/nutritionists/
    Query params: ?search=&specialty=&accepting=true&verified=true
    """
    qs = NutritionistProfile.objects.select_related('user').filter(user__is_active=True)   # hidden by staff → not listed

    if request.query_params.get('accepting') == 'true':
        qs = qs.filter(is_accepting_clients=True)
    if request.query_params.get('verified') == 'true':
        qs = qs.filter(is_verified=True)

    specialty = request.query_params.get('specialty')
    if specialty and specialty != 'All Specialties':
        qs = qs.filter(specialties__contains=[specialty])

    search = request.query_params.get('search')
    if search:
        qs = qs.filter(
            Q(user__first_name__icontains=search) |
            Q(user__last_name__icontains=search) |
            Q(title__icontains=search)
        )

    return Response(NutritionistProfileSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def nutritionist_detail(request, nutritionist_id):
    """
    GET /api/nutrition/nutritionists/<id>/
    """
    nutritionist = get_object_or_404(
        NutritionistProfile.objects.select_related('user'),
        id=nutritionist_id, user__is_active=True,   # hidden by staff → 404, same as the list
    )
    return Response(NutritionistProfileSerializer(nutritionist).data)


# ============================================
# MY PLAN
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_plan(request):
    """
    GET /api/nutrition/plan/
    The member's newest active plan (a member can have one per nutritionist they paid; all of them
    are at GET /api/nutrition/plans/).
    """
    plan = _active_plans(request.user).first()

    if not plan:
        return Response(
            {'detail': 'No active nutrition plan.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    return Response(NutritionPlanSerializer(plan).data)


def _active_plans(member):
    """Every active plan of the member, newest first — one per nutritionist they've paid."""
    return (NutritionPlan.objects.filter(member=member, status='active')
            .select_related('nutritionist__user').order_by('-created_at', '-id'))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_plans(request):
    """GET /api/nutrition/plans/ — all the member's active plans, newest first."""
    return Response(NutritionPlanSerializer(_active_plans(request.user), many=True).data)


# Goal presets used when a member requests a plan
GOAL_DEFAULTS = {
    'hypertrophy': dict(
        target_calories=3200, target_protein_g=200,
        target_carbs_g=380, target_fats_g=90, target_water_ml=4000,
    ),
    'fat_loss': dict(
        target_calories=2000, target_protein_g=180,
        target_carbs_g=150, target_fats_g=60, target_water_ml=3800,
    ),
    'recomp': dict(
        target_calories=2500, target_protein_g=190,
        target_carbs_g=250, target_fats_g=75, target_water_ml=3500,
    ),
    'maintenance': dict(
        target_calories=2700, target_protein_g=160,
        target_carbs_g=300, target_fats_g=85, target_water_ml=3500,
    ),
    'endurance': dict(
        target_calories=3000, target_protein_g=150,
        target_carbs_g=420, target_fats_g=70, target_water_ml=4200,
    ),
}


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def request_plan(request):
    """
    POST /api/nutrition/plan/request/
    Body: { "nutritionist_id": 3, "goal": "hypertrophy" }
    """
    nutritionist_id = request.data.get('nutritionist_id')
    goal = request.data.get('goal', 'hypertrophy')

    if not nutritionist_id:
        return Response(
            {'detail': 'nutritionist_id is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if goal not in GOAL_DEFAULTS:
        return Response(
            {'detail': f'Invalid goal. Choose from: {list(GOAL_DEFAULTS.keys())}'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        nutritionist = NutritionistProfile.objects.get(id=nutritionist_id, user__is_active=True, is_accepting_clients=True)
    except NutritionistProfile.DoesNotExist:
        return Response(
            {'detail': 'This nutritionist isn’t taking new clients.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    defaults = GOAL_DEFAULTS[goal]

    # Same nutritionist, new goal: no new fee — switch the targets and send it back for review.
    current = NutritionPlan.objects.filter(member=request.user, status='active', nutritionist=nutritionist).first()
    if current:
        current.goal = goal
        current.reviewed_at = None
        for field, value in defaults.items():
            setattr(current, field, value)
        current.save()
        return Response(NutritionPlanSerializer(current).data)

    # A new nutritionist: the plan waits for the consultation fee (free when the rate is 0).
    from django.db import transaction as db_transaction
    from apps.payments.gateway import activate_plan
    with db_transaction.atomic():
        # An older unpaid request to the same nutritionist is replaced; requests to others stay.
        NutritionPlan.objects.filter(member=request.user, status='pending_payment',
                                     nutritionist=nutritionist).update(status='archived')
        plan = NutritionPlan.objects.create(
            member=request.user,
            nutritionist=nutritionist,
            goal=goal,
            status='pending_payment',
            price=nutritionist.hourly_rate or 0,
            **defaults,
        )
        if plan.price <= 0:
            activate_plan(plan)

    return Response(
        NutritionPlanSerializer(plan).data,
        status=status.HTTP_201_CREATED,
    )


# ============================================
# MEALS
# ============================================
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def meals(request):
    """
    GET  /api/nutrition/meals/?date=2026-10-11
    POST /api/nutrition/meals/
    """
    if request.method == 'GET':
        qs = MealLog.objects.filter(member=request.user)

        date_filter = request.query_params.get('date')
        if date_filter:
            qs = qs.filter(logged_date=date_filter)

        return Response(MealLogSerializer(qs[:100], many=True).data)

    # POST — log a meal
    serializer = MealLogSerializer(data=request.data)
    if serializer.is_valid():
        serializer.save(member=request.user, plan=_active_plans(request.user).first())
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_meal(request, meal_id):
    """
    DELETE /api/nutrition/meals/<id>/
    """
    try:
        meal = MealLog.objects.get(id=meal_id, member=request.user)
    except MealLog.DoesNotExist:
        return Response(
            {'detail': 'Meal not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    meal.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ============================================
# WATER
# ============================================
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def water(request):
    """
    GET  /api/nutrition/water/       → today's intake
    POST /api/nutrition/water/       → add water (body: { "amount_ml": 250 })
    """
    today = timezone.localdate()
    entry, _ = WaterIntake.objects.get_or_create(
        member=request.user, date=today
    )

    if request.method == 'GET':
        return Response(WaterIntakeSerializer(entry).data)

    # POST — add
    try:
        amount = int(request.data.get('amount_ml', 0))
    except (TypeError, ValueError):
        return Response(
            {'detail': 'Invalid amount.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if amount <= 0:
        return Response(
            {'detail': 'Amount must be positive.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    entry.amount_ml += amount
    entry.save()

    return Response(WaterIntakeSerializer(entry).data)


# ============================================
# DAILY MACRO SUMMARY
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def daily_summary(request):
    """
    GET /api/nutrition/summary/
    Returns today's consumed macros vs targets.
    """
    today = timezone.localdate()
    meals_today = MealLog.objects.filter(
        member=request.user, logged_date=today
    )

    consumed = meals_today.aggregate(
        calories=Sum('calories'),
        protein=Sum('protein_g'),
        carbs=Sum('carbs_g'),
        fats=Sum('fats_g'),
    )

    plan = _active_plans(request.user).first()

    water_entry, _ = WaterIntake.objects.get_or_create(
        member=request.user, date=today
    )

    targets = {}
    if plan:
        targets = {
            'calories': plan.target_calories,
            'protein_g': plan.target_protein_g,
            'carbs_g': plan.target_carbs_g,
            'fats_g': plan.target_fats_g,
            'water_ml': plan.target_water_ml,
        }

    def safe(val):
        return float(val) if val is not None else 0

    return Response({
        'date': str(today),
        'consumed': {
            'calories': consumed['calories'] or 0,
            'protein_g': safe(consumed['protein']),
            'carbs_g': safe(consumed['carbs']),
            'fats_g': safe(consumed['fats']),
            'water_ml': water_entry.amount_ml,
        },
        'targets': targets,
        'meals_logged': meals_today.count(),
    })


# ============================================
# DEXA SCANS
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dexa_history(request):
    """
    GET /api/nutrition/dexa/
    """
    qs = DexaScan.objects.filter(member=request.user)[:20]
    return Response(DexaScanSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def latest_dexa(request):
    """
    GET /api/nutrition/dexa/latest/
    """
    scan = DexaScan.objects.filter(member=request.user).first()
    if not scan:
        return Response(
            {'detail': 'No DEXA scan on record.'},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(DexaScanSerializer(scan).data)