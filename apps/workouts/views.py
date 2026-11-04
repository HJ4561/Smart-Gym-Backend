# apps/workouts/views.py
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Sum, Count, Max, Avg, Q
from datetime import timedelta

from .models import (
    Exercise, Workout, WorkoutExercise, WorkoutSet,
    PersonalRecord, WorkoutTemplate,
)
from .serializers import (
    ExerciseSerializer,
    WorkoutSerializer,
    WorkoutListSerializer,
    WorkoutExerciseSerializer,
    WorkoutSetSerializer,
    PersonalRecordSerializer,
    WorkoutTemplateSerializer,
)


# ============================================
# EXERCISES (public catalog)
# ============================================

# Limits for a logged set (shown to the member when they type something out of range).
MAX_WEIGHT_KG = 500
MAX_REPS = 100


def clean_set(data):
    """(values, errors) for weight_kg / reps / rpe typed in the portal."""
    from decimal import Decimal, InvalidOperation
    errors, values = {}, {}
    raw_w = data.get('weight_kg', 0)
    try:
        w = Decimal(str(raw_w if raw_w not in (None, '') else 0))
        if not w.is_finite() or w < 0 or w > MAX_WEIGHT_KG:
            raise InvalidOperation
        if w != w.quantize(Decimal('0.01')):
            errors['weight_kg'] = 'Use at most 2 decimals (e.g. 22.5).'
        values['weight_kg'] = w.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        errors['weight_kg'] = f'Enter a weight between 0 and {MAX_WEIGHT_KG} kg.'
    raw_r = data.get('reps')
    try:
        r = Decimal(str(raw_r))
        if not r.is_finite() or r != r.to_integral_value() or not 1 <= r <= MAX_REPS:
            raise InvalidOperation
        values['reps'] = int(r)
    except (InvalidOperation, ValueError, TypeError):
        errors['reps'] = f'Enter whole reps between 1 and {MAX_REPS}.'
    raw_rpe = data.get('rpe')
    if raw_rpe not in (None, ''):
        try:
            rpe = Decimal(str(raw_rpe))
            if not rpe.is_finite() or not 1 <= rpe <= 10:
                raise InvalidOperation
            values['rpe'] = rpe.quantize(Decimal('0.1'))
        except (InvalidOperation, ValueError, TypeError):
            errors['rpe'] = 'RPE must be between 1 and 10.'
    if errors:
        errors['detail'] = ' '.join(v for k, v in errors.items())
    return values, errors


@api_view(['GET'])
@permission_classes([AllowAny])
def exercises_list(request):
    """
    GET /api/workouts/exercises/
    Query params: ?category=strength&muscle=chest&search=press
    """
    qs = Exercise.objects.filter(is_active=True)

    category = request.query_params.get('category')
    if category:
        qs = qs.filter(category=category)

    muscle = request.query_params.get('muscle')
    if muscle:
        qs = qs.filter(primary_muscle=muscle)

    search = request.query_params.get('search')
    if search:
        qs = qs.filter(name__icontains=search)

    return Response(ExerciseSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def exercise_by_qr(request, qr_tag_id):
    """
    GET /api/workouts/exercises/qr/<qr_tag_id>/
    Find exercise by QR tag.
    """
    tag = qr_tag_id.strip()
    exercise = Exercise.objects.filter(qr_tag_id__iexact=tag, is_active=True).first() if tag else None
    if exercise is None:
        return Response(
            {'detail': 'No exercise found for this QR tag.'},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(ExerciseSerializer(exercise).data)


# ============================================
# WORKOUTS
# ============================================
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def workouts_list(request):
    """
    GET  /api/workouts/        → list user's workouts
    POST /api/workouts/        → start a new workout
    """
    if request.method == 'GET':
        qs = Workout.objects.filter(member=request.user)[:50]
        return Response(WorkoutListSerializer(qs, many=True).data)

    serializer = WorkoutSerializer(data=request.data)
    if serializer.is_valid():
        serializer.save(member=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def workout_detail(request, workout_id):
    """
    GET    /api/workouts/<id>/
    PATCH  /api/workouts/<id>/
    DELETE /api/workouts/<id>/
    """
    try:
        workout = Workout.objects.get(id=workout_id, member=request.user)
    except Workout.DoesNotExist:
        return Response(
            {'detail': 'Workout not found.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == 'GET':
        return Response(WorkoutSerializer(workout).data)

    if request.method == 'PATCH':
        serializer = WorkoutSerializer(workout, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    workout.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def workout_complete(request, workout_id):
    """
    POST /api/workouts/<id>/complete/
    """
    try:
        workout = Workout.objects.get(id=workout_id, member=request.user)
    except Workout.DoesNotExist:
        return Response({'detail': 'Workout not found.'}, status=status.HTTP_404_NOT_FOUND)

    workout.status = 'completed'
    workout.completed_at = timezone.now()

    if workout.started_at:
        duration = (workout.completed_at - workout.started_at).total_seconds() / 60
        workout.duration_minutes = int(duration)

    sets = WorkoutSet.objects.filter(workout_exercise__workout=workout)
    workout.total_sets = sets.count()
    workout.total_reps = sets.aggregate(total=Sum('reps'))['total'] or 0
    tonnage = sum((s.reps * s.weight_kg) for s in sets)
    workout.total_tonnage_kg = tonnage
    workout.total_exercises = workout.exercises.count()

    vels = [s.mean_velocity for s in sets if s.mean_velocity]
    if vels:
        workout.avg_velocity = sum(vels) / len(vels)

    # Rough estimate (no wearable): ~6 kcal per minute of resistance training (MET 5, ~70 kg).
    if workout.duration_minutes and not workout.calories_burned:
        workout.calories_burned = int(min(workout.duration_minutes, 240) * KCAL_PER_MINUTE)

    workout.save()
    return Response(WorkoutSerializer(workout).data)


# ============================================
# WORKOUT EXERCISES
# ============================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def add_exercise_to_workout(request, workout_id):
    """
    POST /api/workouts/<id>/exercises/
    Body: { "exercise_id": 5, "order": 0, "rest_seconds": 90 }
    """
    try:
        workout = Workout.objects.get(id=workout_id, member=request.user)
    except Workout.DoesNotExist:
        return Response({'detail': 'Workout not found.'}, status=status.HTTP_404_NOT_FOUND)

    serializer = WorkoutExerciseSerializer(data=request.data)
    if serializer.is_valid():
        serializer.save(workout=workout)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================
# WORKOUT SETS
# ============================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def log_set(request, workout_exercise_id):
    """
    POST /api/workouts/exercises/<workout_exercise_id>/sets/
    Body: { "set_number": 1, "reps": 8, "weight_kg": 80, "mean_velocity": 0.82, "rpe": 8 }
    """
    try:
        we = WorkoutExercise.objects.get(
            id=workout_exercise_id,
            workout__member=request.user,
        )
    except WorkoutExercise.DoesNotExist:
        return Response({'detail': 'Workout exercise not found.'}, status=status.HTTP_404_NOT_FOUND)

    values, errors = clean_set(request.data)
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    serializer = WorkoutSetSerializer(data={**dict(request.data.items()), **{k: str(v) for k, v in values.items()}})
    if serializer.is_valid():
        workout_set = serializer.save(workout_exercise=we)

        estimated_1rm = float(workout_set.weight_kg) * (1 + workout_set.reps / 30)
        pr, created = PersonalRecord.objects.get_or_create(
            member=request.user,
            exercise=we.exercise,
            defaults={
                'best_weight_kg': workout_set.weight_kg,
                'best_reps': workout_set.reps,
                'best_estimated_1rm': estimated_1rm,
            },
        )
        if not created:
            if estimated_1rm > float(pr.best_estimated_1rm):
                pr.best_weight_kg = workout_set.weight_kg
                pr.best_reps = workout_set.reps
                pr.best_estimated_1rm = estimated_1rm
                pr.save()
                workout_set.is_pr = True
                workout_set.save()
                _notify_pr(request.user, we.exercise, workout_set)

        return Response(WorkoutSetSerializer(workout_set).data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================
# PERSONAL RECORDS
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_prs(request):
    """
    GET /api/workouts/prs/
    """
    prs = PersonalRecord.objects.filter(member=request.user)
    return Response(PersonalRecordSerializer(prs, many=True).data)


# ============================================
# ANALYTICS / STATS
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def workout_stats(request):
    """
    GET /api/workouts/stats/
    """
    user = request.user
    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)

    workouts = Workout.objects.filter(member=user, status='completed')
    recent = workouts.filter(completed_at__gte=thirty_days_ago)

    streak = 0
    today = timezone.localdate()
    check_date = today
    while Workout.objects.filter(
        member=user, status='completed', completed_at__date=check_date
    ).exists():
        streak += 1
        check_date -= timedelta(days=1)

    recent_sets = WorkoutSet.objects.filter(
        workout_exercise__workout__in=recent,
    )

    return Response({
        'total_workouts': workouts.count(),
        'total_tonnage_all_time': float(
            workouts.aggregate(t=Sum('total_tonnage_kg'))['t'] or 0
        ),
        'recent_30d': {
            'workouts': recent.count(),
            'total_sets': recent_sets.count(),
            'total_reps': recent_sets.aggregate(t=Sum('reps'))['t'] or 0,
            'total_tonnage_kg': float(recent.aggregate(t=Sum('total_tonnage_kg'))['t'] or 0),
        },
        'current_streak_days': streak,
        'total_prs': PersonalRecord.objects.filter(member=user).count(),
        'avg_duration_minutes': recent.aggregate(a=Avg('duration_minutes'))['a'] or 0,
    })


# ============================================
# WORKOUT TEMPLATES
# ============================================
@api_view(['GET'])
@permission_classes([AllowAny])
def templates_list(request):
    """
    GET /api/workouts/templates/
    """
    qs = WorkoutTemplate.objects.filter(is_active=True, is_public=True)

    category = request.query_params.get('category')
    if category:
        qs = qs.filter(category=category)

    difficulty = request.query_params.get('difficulty')
    if difficulty:
        qs = qs.filter(difficulty=difficulty)

    return Response(WorkoutTemplateSerializer(qs, many=True).data)


# ============================================
# QUICK LOG SET FROM MACHINE GUIDE
# ============================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def quick_log_set(request, exercise_id):
    """
    POST /api/workouts/exercises/<exercise_id>/quick-log/
    Body: { "weight_kg": 75, "reps": 10, "mean_velocity": 0.82, "rpe": 8 }

    Auto-creates a workout if none in progress, adds the exercise, logs the set.
    Returns the log entry + any new PR.
    """
    try:
        exercise = Exercise.objects.get(id=exercise_id)
    except Exercise.DoesNotExist:
        return Response({'detail': 'Exercise not found.'}, status=status.HTTP_404_NOT_FOUND)

    values, errors = clean_set(request.data)
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)

    workout = Workout.objects.filter(
        member=request.user,
        status='in_progress',
    ).order_by('-started_at').first()

    if not workout:
        workout = Workout.objects.create(
            member=request.user,
            name='Machine Guide Session',
            status='in_progress',
        )

    we, created = WorkoutExercise.objects.get_or_create(
        workout=workout,
        exercise=exercise,
        defaults={'order': workout.exercises.count()},
    )

    set_count = we.sets.count()

    workout_set = WorkoutSet.objects.create(
        workout_exercise=we,
        set_number=set_count + 1,
        reps=values['reps'],
        weight_kg=values['weight_kg'],
        mean_velocity=request.data.get('mean_velocity') or None,
        peak_velocity=request.data.get('peak_velocity') or None,
        rpe=values.get('rpe'),
        is_warmup=request.data.get('is_warmup', False),
    )

    estimated_1rm = float(workout_set.weight_kg) * (1 + workout_set.reps / 30)
    pr_achieved = False

    pr, pr_created = PersonalRecord.objects.get_or_create(
        member=request.user,
        exercise=exercise,
        defaults={
            'best_weight_kg': workout_set.weight_kg,
            'best_reps': workout_set.reps,
            'best_estimated_1rm': estimated_1rm,
        },
    )

    if not pr_created:
        if estimated_1rm > float(pr.best_estimated_1rm):
            pr.best_weight_kg = workout_set.weight_kg
            pr.best_reps = workout_set.reps
            pr.best_estimated_1rm = estimated_1rm
            pr.save()
            workout_set.is_pr = True
            workout_set.save()
            pr_achieved = True
            _notify_pr(request.user, exercise, workout_set)

    return Response({
        'set_id': workout_set.id,
        'workout_id': workout.id,
        'workout_exercise_id': we.id,
        'set_number': workout_set.set_number,
        'reps': workout_set.reps,
        'weight_kg': float(workout_set.weight_kg),
        'is_pr': workout_set.is_pr,
        'pr_achieved': pr_achieved,
    }, status=status.HTTP_201_CREATED)


KCAL_PER_MINUTE = 6


def _notify_pr(member, exercise, workout_set):
    from apps.core.i18n import ar, bi
    from apps.notifications.service import notify
    notify(member, 'achievement', bi('New personal record!', 'رقم شخصي جديد!'),
           bi(f'{exercise.name}: {workout_set.weight_kg:g} kg × {workout_set.reps}.',
              f'{ar(exercise, "name")}: {workout_set.weight_kg:g} كجم × {workout_set.reps}.'), icon='emoji_events',
           action_label='View records', action_path='/dashboard/profile')


# ============================================
# MACHINE VIDEOS (Basic free · Advanced/Premium by plan or purchase)
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def exercise_videos(request, exercise_id):
    """
    GET /api/workouts/exercises/<id>/videos/
    Every video for the machine with whether the member can watch it. Locked videos come without
    their link, with the price and the plans that include them.
    """
    from . import videos as vids
    from .models import VideoPurchase
    ex = get_object_or_404(Exercise, id=exercise_id, is_active=True)
    level, plan_name = vids.plan_level(request.user)
    bought = set(VideoPurchase.objects.filter(member=request.user).values_list('video_id', flat=True))
    included = {t: vids.plans_including(t) for t in ('advanced', 'premium')}
    rows = []
    if ex.video_url:      # the machine's own walkthrough link (set on the Machines page) is a free video
        rows.append({'id': None, 'tier': 'basic', 'tier_label': vids.TIERS['basic'], 'title': 'Machine walkthrough',
                     'description': '', 'duration_minutes': None, 'price': '0', 'coach': 'Smart Gym',
                     'locked': False, 'access': 'free', 'video_url': ex.video_url, 'included_in': []})
    for v in ex.videos.filter(is_active=True).select_related('coach__user').order_by('created_at'):
        can, why = vids.access(request.user, v, level, bought)
        rows.append(vids.member_row(v, can, why, included.get(v.tier, [])))
    rows.sort(key=lambda r: vids.RANK[r['tier']])
    names = {0: 'Basic videos', 1: 'Basic + Advanced videos', 2: 'All videos'}
    return Response({'exercise_id': ex.id, 'plan': plan_name, 'plan_level': level,
                     'plan_includes': names[level], 'videos': rows})
