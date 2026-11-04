from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Sum, Avg, Count
from datetime import timedelta

from apps.members.models import Membership, CheckIn
from apps.workouts.models import Workout, WorkoutSet, PersonalRecord, Exercise
from apps.notifications.models import Notification
from django.db.models import F

from .occupancy import current_occupancy


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard(request):
    """
    GET /api/dashboard/
    One endpoint, everything the Dashboard needs.
    """
    user = request.user
    now = timezone.now()
    today = timezone.localdate()
    thirty_days_ago = now - timedelta(days=30)

    # ============================================
    # WORKOUT STATS
    # ============================================
    workouts_qs = Workout.objects.filter(member=user)
    completed_workouts = workouts_qs.filter(status='completed')
    recent_workouts = completed_workouts.filter(completed_at__gte=thirty_days_ago)

    # Streak
    streak = 0
    check_date = today
    while completed_workouts.filter(completed_at__date=check_date).exists():
        streak += 1
        check_date -= timedelta(days=1)

    # Total tonnage
    total_tonnage = completed_workouts.aggregate(
        t=Sum('total_tonnage_kg')
    )['t'] or 0

    recent_tonnage = recent_workouts.aggregate(
        t=Sum('total_tonnage_kg')
    )['t'] or 0

    # Avg velocity
    avg_velocity = (
        recent_workouts
        .filter(avg_velocity__isnull=False)
        .aggregate(a=Avg('avg_velocity'))['a'] or 0
    )

    # ============================================
    # RECENT WORKOUTS (for logs)
    # ============================================
    workouts_list = []
    for w in completed_workouts.order_by('-completed_at')[:5]:
        workouts_list.append({
            'id': w.id,
            'name': w.name,
            'status': w.status,
            'started_at': w.started_at.isoformat() if w.started_at else None,
            'completed_at': w.completed_at.isoformat() if w.completed_at else None,
            'duration_minutes': w.duration_minutes,
            'total_exercises': w.total_exercises,
            'total_sets': w.total_sets,
            'total_tonnage_kg': float(w.total_tonnage_kg or 0),
            'is_pr': WorkoutSet.objects.filter(workout_exercise__workout=w, is_pr=True).exists(),
        })

    # ============================================
    # MEMBERSHIP
    # ============================================
    membership_data = None
    try:
        m = Membership.objects.select_related('plan').get(member=user)
        membership_data = {
            'id': m.id,
            'plan': {
                'name': m.plan.display_name,
                'tier': m.plan.name,
            },
            'status': m.status,
            'start_date': m.start_date.isoformat(),   # = the day the registration was approved
            'expiry_date': m.expiry_date.isoformat(),
            'days_remaining': m.days_remaining,
            'time_remaining': m.time_remaining,       # months + days left, % of plan used
            'auto_renew': m.auto_renew,
        }
    except Membership.DoesNotExist:
        pass

    # ============================================
    # NOTIFICATIONS
    # ============================================
    unread_count = Notification.objects.filter(
        recipient=user, is_read=False
    ).count()

    # ============================================
    # TODAY'S WORKOUT (in progress or last completed)
    # ============================================
    in_progress = workouts_qs.filter(status='in_progress').order_by('-started_at').first()
    today_workout = None
    if in_progress:
        # Build with exercises and sets
        exercises = []
        for we in in_progress.exercises.select_related('exercise').all():
            sets_data = [{
                'set_number': s.set_number,
                'reps': s.reps,
                'weight_kg': float(s.weight_kg),
                'is_pr': s.is_pr,
                'is_warmup': s.is_warmup,
            } for s in we.sets.all()]

            exercises.append({
                'id': we.id,
                'name': we.exercise.name, 'name_ar': we.exercise.name_ar,
                'muscle': we.exercise.primary_muscle,
                'sets': sets_data,
                'total_sets': we.sets.count(),
            })

        today_workout = {
            'id': in_progress.id,
            'name': in_progress.name,
            'status': 'in_progress',
            'started_at': in_progress.started_at.isoformat(),
            'total_exercises': in_progress.total_exercises or len(exercises),
            'total_sets': in_progress.total_sets or sum(e['total_sets'] for e in exercises),
            'rpe_avg': float(in_progress.rpe_avg) if in_progress.rpe_avg else None,
            'exercises': exercises,
        }

    # ============================================
    # LAST SESSION (real numbers from the latest completed workout)
    # ============================================
    latest_workout = completed_workouts.order_by('-completed_at').first()
    biometric = None
    if latest_workout:
        biometric = {
            'workout_name': latest_workout.name,
            'completed_at': latest_workout.completed_at.isoformat() if latest_workout.completed_at else None,
            'calories': latest_workout.calories_burned or 0,
            'duration_minutes': latest_workout.duration_minutes or 0,
            'total_sets': latest_workout.total_sets or 0,
            'total_reps': latest_workout.total_reps or 0,
            'tonnage_kg': float(latest_workout.total_tonnage_kg or 0),
            'avg_velocity': float(latest_workout.avg_velocity) if latest_workout.avg_velocity else None,
            'rpe_avg': float(latest_workout.rpe_avg) if latest_workout.rpe_avg else None,
        }

    # ============================================
    # DIRECTIVES (rule-based, each links somewhere real)
    # ============================================
    directives = _directives(user, today, in_progress, completed_workouts, membership_data)

    # ============================================
    # FLOOR DENSITY (live, from open check-ins)
    # ============================================
    occ = current_occupancy()
    floor_density = {
        'overall_percent': occ['percent'],
        'current': occ['current'],
        'capacity': occ['capacity'],
        'label': occ['label'],
        'peak_window': occ['peak_window'],
        'zones': [{'label': z['label'], 'percent': z['percent'], 'count': z['count'], 'accent': i == 0}
                  for i, z in enumerate(occ['zones'])],
    }

    # ============================================
    # RESPONSE
    # ============================================
    return Response({
        'user': {
            'id': user.id,
            'full_name': user.full_name,
            'first_name': user.first_name,
            'avatar': user.avatar.url if user.avatar else None,
        },
        'stats': {
            'total_workouts': completed_workouts.count(),
            'recent_30d': {
                'workouts': recent_workouts.count(),
                'total_tonnage_kg': float(recent_tonnage),
            },
            'total_tonnage_all_time': float(total_tonnage),
            'current_streak_days': streak,
            'total_prs': PersonalRecord.objects.filter(member=user).count(),
            'avg_velocity': float(avg_velocity),
        },
        'membership': membership_data,
        'unread_notifications': unread_count,
        'recent_workouts': workouts_list,
        'today_workout': today_workout,
        'biometric': biometric,
        'directives': directives,
        'floor_density': floor_density,
    })


def _directives(user, today, in_progress, completed_workouts, membership):
    from apps.attendance.models import MemberMilestone
    from apps.catalog.models import Course, Enrollment
    from apps.shop.models import Order

    out = []

    def add(tag, icon, title, body, cta, path, accent=False):
        out.append({'tag': tag, 'tag_color': 'accent' if accent else 'primary', 'icon': icon,
                    'title': title, 'body': body, 'footer': '', 'cta': cta, 'link': path})

    if in_progress:
        add('Workout', 'play_arrow', f'Finish "{in_progress.name}"',
            f'{in_progress.total_sets or 0} sets logged so far. Log your next set or finish the session.',
            'Open Machine Guide', '/dashboard/machine-guide', accent=True)

    if membership and not membership['time_remaining']['expired'] and membership['days_remaining'] <= 14:
        add('Membership', 'card_membership', f"{membership['days_remaining']} days left",
            'Renew now so your access continues without a gap.', 'Renew', '/dashboard/payments', accent=True)
    elif membership and membership['time_remaining']['expired']:
        add('Membership', 'card_membership', 'Membership expired',
            'Renew to get back into the gym.', 'Renew', '/dashboard/payments', accent=True)

    unpaid = Order.objects.filter(member=user, status='pending').order_by('-created_at').first()
    if unpaid:
        add('Shop', 'shopping_bag', f'Order {unpaid.order_number} awaiting payment',
            f'SAR {unpaid.total} — pay to have it prepared.', 'Pay now', '/dashboard/shop?tab=orders')

    ready = MemberMilestone.objects.filter(member=user, status='active',
                                           current_days__gte=F('milestone__target_days')).select_related('milestone').first()
    if ready:
        add('Reward', 'emoji_events', ready.milestone.title, 'Your streak reward is ready to claim.',
            'Claim', '/dashboard/attendance')

    last = completed_workouts.order_by('-completed_at').values_list('completed_at', flat=True).first()
    if last and (today - timezone.localtime(last).date()).days >= 3 and not in_progress:
        gap = (today - timezone.localtime(last).date()).days
        add('Consistency', 'event_repeat', f'{gap} days since your last session',
            'Start a session today to rebuild your streak.', 'Start workout', '/dashboard')

    enrolled = Enrollment.objects.filter(member=user).values_list('course_id', flat=True)
    course = Course.objects.filter(status='published').exclude(id__in=enrolled).order_by('-is_featured', '-rating').first()
    if course:
        add('Course', 'school', course.title, (course.subtitle or course.description or '')[:140],
            'View course', '/dashboard/courses')
    return out



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def occupancy(request):
    """GET /api/dashboard/occupancy/ — live gym load for the portal header."""
    return Response(current_occupancy())
