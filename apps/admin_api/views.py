# apps/admin_api/views.py
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Sum, Count, Avg, Q
from django.utils import timezone
from datetime import timedelta

from .permissions import IsAdminUser


def _range(request, default=30):
    """(start datetime, days, label) from ?range=1d|7d|30d|90d|ytd (1d = today since midnight, gym time)."""
    now = timezone.now()
    r = (request.query_params.get('range') or f'{default}d').lower()
    if r == '1d':
        return timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0), 1, 'Today'
    if r == 'ytd':
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, max(1, (now - start).days), 'Year to date'
    days = {'7d': 7, '30d': 30, '90d': 90}.get(r, default)
    return now - timedelta(days=days), days, f'Last {days} days'


def cash_transactions(qs):
    """Money that actually moved: card / STC Pay payments, top-ups and card refunds.
    Spending (or refunding into) locker credit is excluded: that money was counted at top-up."""
    return qs.filter(status='settled').exclude(payment_method_label='Locker credit')
from apps.accounts.models import User
from apps.members.models import Membership, MembershipPlan, CheckIn
from apps.workouts.models import Workout, WorkoutSet, PersonalRecord, Exercise
from apps.catalog.models import TrainerProfile, Course, Enrollment, TrainerSession
from apps.payments.models import Transaction, Invoice, LockerCredit
from apps.shop.models import Product, Order
from apps.nutrition.models import NutritionistProfile, NutritionPlan


# ============================================
# OVERVIEW / DASHBOARD
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminUser])
def overview(request):
    """
    GET /api/admin/overview/?range=7d|30d|90d|ytd
    """
    now = timezone.now()
    thirty_days_ago = now - timedelta(days=30)
    since, days, range_label = _range(request)

    total_members = User.objects.filter(role='member').count()
    active_members = Membership.objects.filter(status='active').count()
    expiring_soon = Membership.objects.filter(
        status='active',
        expiry_date__lte=(timezone.localdate() + timedelta(days=14))
    ).count()

    recent_txns = cash_transactions(Transaction.objects.filter(created_at__gte=since))
    revenue_30d = recent_txns.aggregate(t=Sum('amount'))['t'] or 0

    workouts_30d = Workout.objects.filter(
        status='completed',
        completed_at__gte=thirty_days_ago,
    ).count()

    today = timezone.localdate()
    check_ins_today = CheckIn.objects.filter(check_in_time__date=today).count()

    active_courses = Course.objects.filter(status='published').count()
    pending_courses = Course.objects.filter(status='pending').count()

    orders_30d = Order.objects.filter(created_at__gte=thirty_days_ago).count()

    return Response({
        'members': {
            'total': total_members,
            'active_memberships': active_members,
            'expiring_soon': expiring_soon,
        },
        'revenue': {
            'last_30d': float(revenue_30d),          # = the selected range (name kept for the frontend)
            'transaction_count': recent_txns.count(),
            'range_days': days,
            'range_label': range_label,
        },
        'activity': {
            'workouts_30d': workouts_30d,
            'check_ins_today': check_ins_today,
            'orders_30d': orders_30d,
        },
        'content': {
            'active_courses': active_courses,
            'pending_approvals': pending_courses,
        },
    })


# ============================================
# FINANCE ADMIN
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminUser])
def transactions_export(request):
    """
    GET /api/admin/finance/transactions/export/?range=7d|30d|90d|ytd → CSV of every
    transaction in the range (all payment methods, including locker credit and refunds).
    """
    import csv
    from django.http import HttpResponse
    since, days, label = _range(request)
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="smart-gym-transactions-{timezone.localdate()}.csv"'
    resp.write('﻿')   # Excel-friendly UTF-8
    w = csv.writer(resp)
    from apps.core.bilingual import header
    w.writerow(header(['Date', 'Transaction', 'Member', 'Email', 'Description', 'Type', 'Paid with',
                'Amount (SAR)', 'Status', 'Counted as revenue']))
    for t in Transaction.objects.select_related('member').filter(created_at__gte=since).order_by('created_at'):
        w.writerow([timezone.localtime(t.created_at).strftime('%Y-%m-%d %H:%M'), t.transaction_id, t.member.full_name,
                    t.member.email, t.description, t.get_transaction_type_display(), t.payment_method_label,
                    f'{t.amount:.2f}', t.status,
                    'yes' if t.status == 'settled' and t.payment_method_label != 'Locker credit' else 'no'])
    return resp


# ============================================
# FLOW + REVENUE (dashboard)
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminUser])
def flow_and_revenue(request):
    """
    GET /api/admin/flow-and-revenue/
    """
    from django.db.models.functions import ExtractHour

    today = timezone.localdate()

    today_hours = (
        CheckIn.objects
        .filter(check_in_time__date=today)
        .annotate(hour=ExtractHour('check_in_time'))
        .values('hour')
        .annotate(count=Count('id'))
    )
    today_map = {row['hour']: row['count'] for row in today_hours}
    today_curve = [{'hour': h, 'count': today_map.get(h, 0)} for h in range(24)]

    thirty_days_ago = today - timedelta(days=30)
    avg_rows = (
        CheckIn.objects
        .filter(check_in_time__date__gte=thirty_days_ago,
                check_in_time__date__lt=today)
        .annotate(hour=ExtractHour('check_in_time'))
        .values('hour')
        .annotate(count=Count('id'))
    )
    avg_map = {row['hour']: row['count'] / 30 for row in avg_rows}
    avg_curve = [
        {'hour': h, 'count': round(avg_map.get(h, 0), 1)}
        for h in range(24)
    ]

    since, days, range_label = _range(request)
    recent = cash_transactions(Transaction.objects.filter(created_at__gte=since))
    STREAMS = {'subscription': 'Memberships & add-ons', 'retail': 'Shop', 'coaching': 'Trainers & courses',
               'topup': 'Locker credit top-ups', 'refund': 'Refunds'}
    REVENUE_COLORS = {'Memberships & add-ons': 'bg-primary-container', 'Shop': 'bg-emerald-400',
                      'Trainers & courses': 'bg-primary', 'Locker credit top-ups': 'bg-amber-400',
                      'Refunds': 'bg-error', 'Other': 'bg-surface-variant'}
    buckets = {}
    for row in recent.values('transaction_type').annotate(total=Sum('amount')):
        key = STREAMS.get(row['transaction_type'], 'Other')
        buckets[key] = buckets.get(key, 0) + float(row['total'])
    total_revenue = sum(buckets.values())
    gross = sum(v for v in buckets.values() if v > 0)
    revenue_streams = [
        {
            'label': k,
            'value': round(v, 2),
            'percent': round(abs(v) / gross * 100, 1) if gross else 0,
            'color': REVENUE_COLORS.get(k, 'bg-surface-variant'),
        }
        for k, v in sorted(buckets.items(), key=lambda x: -x[1])
    ]
    arr = total_revenue / days * 365
    paying = recent.filter(amount__gt=0).values('member').distinct().count()
    return Response({
        'flow': {
            'today': today_curve,
            'rolling_mean': avg_curve,
        },
        'revenue_streams': revenue_streams,
        'summary': {
            'total_30d': round(total_revenue, 2),     # = the selected range
            'range_label': range_label,
            'arr': round(arr, 2),                      # annual run rate at this pace
            'avg_per_member': round(total_revenue / paying, 2) if paying else 0,   # per paying member
        },
    })


# ============================================
# ARENA HEATMAP
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminUser])
def arena_heatmap(request):
    """
    GET /api/admin/arena-heatmap/
    """
    today = timezone.localdate()

    rows = (
        CheckIn.objects
        .filter(check_in_time__date=today)
        .values('facility', 'zone')
        .annotate(count=Count('id'), avg_duration=Avg('duration_minutes'))
    )

    active_now = CheckIn.objects.filter(
        check_in_time__date=today, check_out_time__isnull=True
    ).count()

    total_today = sum(r['count'] for r in rows) or 1

    ZONE_ICONS = {
        'Strength Arena': 'fitness_center',
        'Cardio Deck': 'directions_run',
        'Functional Rig': 'sports_gymnastics',
        'Free Weights': 'weight',
        'Studio': 'self_improvement',
        'Locker Room': 'lock',
        'Recovery Zone': 'spa',
    }

    zones = []
    for i, r in enumerate(rows):
        zone_name = r['zone'] or r['facility'] or 'General Floor'
        load = round((r['count'] / total_today) * 100, 1)

        if load >= 85:
            sector_label = 'CRITICAL'
            bar_color = 'bg-error'
            sector_label_accent = True
        elif load >= 60:
            sector_label = 'ELEVATED'
            bar_color = 'bg-primary'
            sector_label_accent = False
        else:
            sector_label = 'NOMINAL'
            bar_color = 'bg-emerald-400'
            sector_label_accent = False

        avg_dur = int(r['avg_duration'] or 0)

        zones.append({
            'sector': f'SECTOR-{i+1:02d}',
            'sectorLabel': sector_label,
            'title': zone_name,
            'description': f"{r['count']} entries today",
            'load': f'{int(load)}%',
            'width': min(100, load),
            'barColor': bar_color,
            'dwell': f'Avg {avg_dur} min',
            'footerLeft': r['facility'],
            'footerRight': f'{r["count"]} visits',
            'icon': ZONE_ICONS.get(zone_name, 'meeting_room'),
            'topBar': bar_color,
            'sectorAccent': load >= 85,
            'sectorLabelAccent': sector_label_accent,
            'footerRightAccent': 'error' if load >= 85 else 'primary' if load >= 60 else 'none',
        })

    return Response({
        'zones': zones,
        'active_now': active_now,
        'total_today': total_today,
    })


# ============================================
# AUDIT FEED
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminUser])
def audit_feed(request):
    """
    GET /api/admin/audit-feed/
    """
    limit = int(request.query_params.get('limit', 20))
    events = []

    for c in CheckIn.objects.select_related('member').order_by('-check_in_time')[:10]:
        events.append({
            'time': c.check_in_time.strftime('%H:%M:%S'),
            'timestamp': c.check_in_time,
            'node': c.zone or c.facility,
            'event': f'{c.member.email} checked in',
            'detail': f'Facility: {c.facility}',
            'status': 'ENTRY',
            'statusType': 'ok',
            'isAlert': False,
        })

    for t in Transaction.objects.select_related('member').order_by('-created_at')[:10]:
        events.append({
            'time': t.created_at.strftime('%H:%M:%S'),
            'timestamp': t.created_at,
            'node': 'PAYMENT GATEWAY',
            'event': t.description,
            'detail': f'{t.member.email} — ${float(t.amount):.2f}',
            'status': t.status.upper(),
            'statusType': 'ok' if t.status == 'settled' else 'default',
            'isAlert': t.status != 'settled',
        })

    for o in Order.objects.select_related('member').order_by('-created_at')[:5]:
        events.append({
            'time': o.created_at.strftime('%H:%M:%S'),
            'timestamp': o.created_at,
            'node': 'SHOP ORDER',
            'event': f'Order {o.order_number}',
            'detail': f'{o.member.email} — ${float(o.total):.2f}',
            'status': o.status.upper(),
            'statusType': 'ok' if o.status in ('paid', 'completed') else 'default',
            'isAlert': o.status in ('cancelled', 'refunded'),
        })

    events.sort(key=lambda x: x['timestamp'], reverse=True)

    for e in events:
        e['timestamp'] = e['timestamp'].isoformat()

    return Response(events[:limit])


# ============================================
# FACILITY NODES
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminUser])
def facility_nodes(request):
    """
    GET /api/admin/facility-nodes/ — load per facility from open check-ins (matched on the
    check-in's zone/facility name) against the capacities set in Admin → Facilities.
    """
    from apps.dashboard.occupancy import STALE_HOURS
    from apps.members.models import FacilityAddon
    today = timezone.localdate()
    fresh = timezone.now() - timedelta(hours=STALE_HOURS)
    todays = CheckIn.objects.filter(check_in_time__date=today)
    nodes = []
    for a in FacilityAddon.objects.select_related('item').filter(item__collection='facilities', item__is_active=True):
        name = a.name
        match = Q(zone__iexact=name) | Q(facility__iexact=name)
        active = CheckIn.objects.filter(match, check_out_time__isnull=True, check_in_time__gte=fresh).count()
        util = round(active / a.capacity * 100, 1) if a.capacity else 0
        nodes.append({
            'name': name, 'active_now': active, 'visits_today': todays.filter(match).count(),
            'capacity': a.capacity, 'utilization': util,
            'status': 'congested' if util >= 85 else 'elevated' if util >= 60 else 'optimal',
        })
    return Response(nodes)
