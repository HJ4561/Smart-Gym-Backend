from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Avg, Sum, Count
from datetime import timedelta, date
from calendar import monthrange
import csv
from io import StringIO
from django.http import HttpResponse

from .models import Milestone, MemberMilestone
from .serializers import MilestoneSerializer
from apps.members.models import CheckIn


# ============================================
# SUMMARY
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def summary(request):
    """
    GET /api/attendance/summary/
    """
    user = request.user
    today = timezone.localdate()
    month_start = today.replace(day=1)

    all_ci = CheckIn.objects.filter(member=user)
    month_ci = all_ci.filter(check_in_time__date__gte=month_start)

    days = sorted(set(all_ci.values_list('check_in_time__date', flat=True)))
    streak = _current_streak(days, today)
    best = _longest_streak(days)

    total_minutes = all_ci.aggregate(t=Sum('duration_minutes'))['t'] or 0
    month_minutes = month_ci.aggregate(t=Sum('duration_minutes'))['t'] or 0
    timed_visits = all_ci.filter(duration_minutes__isnull=False).count()

    # Usual arrival time: the most common check-in hour over the last 60 days.
    recent = all_ci.filter(check_in_time__date__gte=today - timedelta(days=60))
    hours = {}
    for t in recent.values_list('check_in_time', flat=True):
        h = timezone.localtime(t).hour
        hours[h] = hours.get(h, 0) + 1
    usual_hour = max(hours, key=hours.get) if hours else None
    last = all_ci.order_by('-check_in_time').first()

    month_count = month_ci.count()
    days_in_month = monthrange(today.year, today.month)[1]
    return Response({
        'current_streak': {
            'value': streak,
            'unit': 'Day' if streak == 1 else 'Days',
            'footer': f'Personal best: {best} day{"" if best == 1 else "s"}',
            'progress': min(100, int(streak / best * 100)) if best else 0,
        },
        'month_check_ins': {
            'value': month_count,
            'unit': 'Visit' if month_count == 1 else 'Visits',
            'badge': None,
            'footer': f'{today:%B}',
            'footer_right': f'{today.day}/{days_in_month} days in',
        },
        'workout_hours': {
            'value': round(total_minutes / 60, 1),
            'unit': 'Hrs',
            'badge': None,
            'footer': f'This month: {round(month_minutes / 60, 1)}h',
            'footer_right': f'Avg {int(total_minutes / timed_visits)}m' if timed_visits else '',
        },
        'prime_window': {
            'value': f'{usual_hour:02d}:00' if usual_hour is not None else '—',
            'unit': '',
            'badge': None,
            'footer': 'Your usual arrival (60 days)' if usual_hour is not None else 'No visits yet',
            'footer_right': f'Last visit {timezone.localtime(last.check_in_time):%d %b}' if last else '',
        },
        'total_check_ins': all_ci.count(),
    })


def _current_streak(days, today):
    """Consecutive days with a visit, ending today (or yesterday, so a streak survives until tonight)."""
    have = set(days)
    d = today if today in have else today - timedelta(days=1)
    streak = 0
    while d in have:
        streak += 1
        d -= timedelta(days=1)
    return streak


def _longest_streak(days):
    best = run = 0
    prev = None
    for d in days:
        run = run + 1 if prev and (d - prev).days == 1 else 1
        best = max(best, run)
        prev = d
    return best


# ============================================
# CHECK-INS CRUD
# ============================================
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def check_ins(request):
    """
    GET  /api/attendance/check-ins/    → list
    POST /api/attendance/check-ins/    → create
    """
    if request.method == 'GET':
        qs = CheckIn.objects.filter(member=request.user)[:100]
        return Response([{
            'id': c.id,
            'facility': c.facility,
            'zone': c.zone,
            'check_in_time': c.check_in_time,
            'check_out_time': c.check_out_time,
            'duration_minutes': c.duration_minutes,
        } for c in qs])

    # Check-ins are recorded at the entrance (face gate / staff scanning the pass).
    return Response({'detail': 'Check-ins are recorded at the entrance — show your digital pass or use the face gate.'},
                    status=405)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def check_out(request, check_in_id):
    """
    POST /api/attendance/check-ins/<id>/check-out/
    """
    try:
        ci = CheckIn.objects.get(id=check_in_id, member=request.user)
    except CheckIn.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    if ci.check_out_time:
        return Response({'detail': 'Already checked out.'}, status=400)

    ci.check_out_time = timezone.now()
    duration = (ci.check_out_time - ci.check_in_time).total_seconds() / 60
    ci.duration_minutes = int(duration)
    ci.save()

    return Response({
        'id': ci.id,
        'duration_minutes': ci.duration_minutes,
    })


# ============================================
# LEDGER (paginated + filterable)
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ledger(request):
    """
    GET /api/attendance/ledger/?page=1&per_page=5&zone=&from=&to=
    """
    page = int(request.query_params.get('page', 1))
    per_page = int(request.query_params.get('per_page', 5))

    qs = CheckIn.objects.filter(member=request.user).order_by('-check_in_time')

    # Filters
    zone = request.query_params.get('zone')
    if zone:
        qs = qs.filter(zone__icontains=zone)

    from_date = request.query_params.get('from')
    if from_date:
        qs = qs.filter(check_in_time__date__gte=from_date)

    to_date = request.query_params.get('to')
    if to_date:
        qs = qs.filter(check_in_time__date__lte=to_date)

    total = qs.count()
    start = (page - 1) * per_page
    end = start + per_page
    page_items = list(qs[start:end])

    # Workout volume logged on the same days (sets + kg moved).
    from apps.workouts.models import Workout
    day_volume = {}
    if page_items:
        dates = {timezone.localtime(c.check_in_time).date() for c in page_items}
        for w in Workout.objects.filter(member=request.user, status='completed',
                                        completed_at__date__gte=min(dates), completed_at__date__lte=max(dates)):
            d = timezone.localtime(w.completed_at).date()
            sets, kg = day_volume.get(d, (0, 0))
            day_volume[d] = (sets + (w.total_sets or 0), kg + float(w.total_tonnage_kg or 0))

    results = []
    for c in page_items:
        in_time = c.check_in_time
        out_time = c.check_out_time
        duration = c.duration_minutes

        is_active = out_time is None
        day = timezone.localtime(in_time).date()
        sets, tonnage = day_volume.get(day, (0, 0))

        results.append({
            'id': c.id,
            'date': timezone.localtime(in_time).strftime('%b %d, %Y'),
            'status': 'In gym' if is_active else 'Completed',
            'statusType': 'active' if is_active else 'default',
            'entry': timezone.localtime(in_time).strftime('%H:%M:%S'),
            'exit': 'ACTIVE SESSION' if is_active else timezone.localtime(out_time).strftime('%H:%M:%S'),
            'exitActive': is_active,
            'duration': f'{duration}m' if duration else 'Active',
            'durationActive': is_active,
            'zones': [c.zone] if c.zone else [c.facility],
            'reps': f'{sets} sets' if sets else '—',
            'volume': f'{int(tonnage):,} kg' if tonnage else '',
        })

    return Response({
        'results': results,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ledger_export(request):
    """
    GET /api/attendance/ledger/export/ → CSV download
    """
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="attendance_ledger.csv"'

    writer = csv.writer(response)
    writer.writerow(['Date', 'Entry', 'Exit', 'Duration (min)', 'Facility', 'Zone'])

    for c in CheckIn.objects.filter(member=request.user).order_by('-check_in_time'):
        writer.writerow([
            c.check_in_time.strftime('%Y-%m-%d'),
            c.check_in_time.strftime('%H:%M'),
            c.check_out_time.strftime('%H:%M') if c.check_out_time else 'ACTIVE',
            c.duration_minutes or 0,
            c.facility,
            c.zone,
        ])

    return response


# ============================================
# HEATMAP
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def heatmap(request):
    """
    GET /api/attendance/heatmap/?year=2025
    Returns 52 weeks × 7 days matrix with intensity level 0-4.
    """
    year = int(request.query_params.get('year', timezone.localdate().year))
    today = timezone.localdate()

    # Build 52-week grid aligned to Monday start
    start_date = date(year, 1, 1)
    # Align to Monday
    while start_date.weekday() != 0:
        start_date -= timedelta(days=1)

    # Count check-ins per day
    daily_counts = {}
    ci_qs = CheckIn.objects.filter(
        member=request.user,
        check_in_time__year=year,
    ).values('check_in_time__date').annotate(count=Count('id'))

    for row in ci_qs:
        daily_counts[row['check_in_time__date']] = row['count']

    weeks = []
    cursor = start_date
    for w in range(52):
        week = []
        for d in range(7):
            day = cursor + timedelta(days=d)
            count = daily_counts.get(day, 0)
            # Intensity 0-4
            if count == 0:
                level = 0
            elif count == 1:
                level = 2
            elif count == 2:
                level = 3
            else:
                level = 4
            week.append({
                'date': day.isoformat(),
                'count': count,
                'level': level,
                'is_future': day > today,
            })
        weeks.append(week)
        cursor += timedelta(days=7)

    return Response({
        'year': year,
        'weeks': weeks,
    })


# ============================================
# MILESTONES
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def milestones(request):
    """
    GET /api/attendance/milestones/
    """
    qs = Milestone.objects.filter(is_active=True)
    return Response(MilestoneSerializer(qs, many=True, context={'request': request}).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def claim_milestone(request, milestone_id):
    """
    POST /api/attendance/milestones/<id>/claim/
    """
    try:
        milestone = Milestone.objects.get(id=milestone_id)
    except Milestone.DoesNotExist:
        return Response({'detail': 'Milestone not found.'}, status=404)

    mm, _ = MemberMilestone.objects.get_or_create(
        member=request.user,
        milestone=milestone,
    )

    if mm.status == 'claimed':
        return Response({'detail': 'Already claimed.'}, status=400)
    if mm.current_days < milestone.target_days:
        return Response(
            {'detail': f'Keep going — {milestone.target_days - mm.current_days} more day(s) to unlock this reward.'},
            status=400,
        )

    mm.status = 'claimed'
    mm.claimed_at = timezone.now()
    mm.save()

    return Response({
        'id': mm.id,
        'milestone': milestone.title,
        'status': 'claimed',
        'reward_code': milestone.reward_code,
    })


# ============================================
# HOURLY CURVE
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def hourly(request):
    """
    GET /api/attendance/hourly/
    Returns a 24h histogram of the member's check-in times
    + the campus occupancy curve (all members).
    """
    hours = list(range(24))
    member_buckets = {h: 0 for h in hours}
    campus_buckets = {h: 0 for h in hours}

    since = timezone.now() - timedelta(days=90)
    for t in CheckIn.objects.filter(member=request.user).values_list('check_in_time', flat=True):
        member_buckets[timezone.localtime(t).hour] += 1
    for t in CheckIn.objects.filter(check_in_time__gte=since).values_list('check_in_time', flat=True):
        campus_buckets[timezone.localtime(t).hour] += 1

    # Quietest busy-enough hour: among hours that see visits, the one with the fewest.
    open_hours = [h for h in hours if campus_buckets[h] > 0]
    quiet = min(open_hours, key=lambda h: campus_buckets[h]) if len(open_hours) > 1 else None

    return Response({
        'member': [{'hour': h, 'count': member_buckets[h]} for h in hours],
        'campus': [{'hour': h, 'count': campus_buckets[h]} for h in hours],
        'peak_hour': max(member_buckets, key=member_buckets.get) if any(member_buckets.values()) else None,
        'campus_peak_hour': max(campus_buckets, key=campus_buckets.get) if any(campus_buckets.values()) else None,
        'quiet_hour': quiet,
    })


# ============================================
# ARENAS (zones/facilities snapshot)
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def arenas(request):
    """
    GET /api/attendance/arenas/
    Top 3 arenas the member visits most.
    """
    from apps.facilities.models import Facility

    # Aggregate check-ins per zone
    zone_counts = (
        CheckIn.objects
        .filter(member=request.user)
        .exclude(zone='')
        .values('zone')
        .annotate(count=Count('id'))
        .order_by('-count')[:3]
    )

    snapshots = []
    for z in zone_counts:
        facility = Facility.objects.filter(name__icontains=z['zone'].split(' ')[0]).first()
        snapshots.append({
            'tag': 'Zone Leaderboard' if len(snapshots) == 0 else 'Zone Focus',
            'tagAccent': len(snapshots) == 0,
            'title': z['zone'],
            'description': f'{z["count"]} check-ins recorded at this zone.',
            'image': '',  # will be filled by frontend if needed
            'count': z['count'],
        })

    return Response(snapshots)


# ============================================
# AUTO MILESTONE UPDATER
# ============================================
def _update_streak_milestones(user):
    """Called after each check-in. Updates streak-based milestone progress."""
    today = timezone.localdate()
    days = sorted(set(CheckIn.objects.filter(member=user).values_list('check_in_time__date', flat=True)))
    streak = _current_streak(days, today)

    for milestone in Milestone.objects.filter(is_active=True):
        mm, _ = MemberMilestone.objects.get_or_create(member=user, milestone=milestone)
        if mm.status == 'claimed':
            continue
        was_ready = mm.current_days >= milestone.target_days
        mm.current_days = streak
        mm.progress = min(100, int((streak / milestone.target_days) * 100)) if milestone.target_days else 100
        mm.status = 'locked' if streak == 0 else 'active'
        if streak >= milestone.target_days and not was_ready:
            from apps.core.i18n import ar, bi
            from apps.notifications.service import notify
            notify(user, 'achievement', bi('Reward unlocked', 'تم فتح مكافأة'),
                   bi(f'{milestone.title}: you reached a {milestone.target_days}-day streak. Claim your reward!',
                      f'{ar(milestone, "title")}: وصلت إلى {milestone.target_days} يومًا متتاليًا. استلم مكافأتك!'),
                   icon='emoji_events', action_label='Claim', action_path='/dashboard/attendance')
        mm.save()