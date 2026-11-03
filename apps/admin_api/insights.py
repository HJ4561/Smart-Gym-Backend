"""
Admin → Insights: business KPIs, 12-month member movement, and rule-based recommendations,
each linking to the page where you act on it. Every figure is computed from live data.
"""
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.models import User
from apps.catalog.models import TrainerProfile
from apps.classes.models import GymClass
from apps.members.models import CheckIn, Membership, MembershipRegistration as Reg
from apps.payments.models import PaymentIntent
from apps.shop.models import Product
from apps.workouts.models import Exercise

from .permissions import IsAdminUser

AT_RISK_DAYS = 21
MAX_HIGHLIGHT = 200      # ids carried in a link; past this the page just shows its list


def with_highlight(path, ids):
    """`path` plus ?highlight=1,2,3 — the page it opens marks those rows."""
    if not ids or len(ids) > MAX_HIGHLIGHT:
        return path
    return path + ('&' if '?' in path else '?') + 'highlight=' + ','.join(str(int(i)) for i in ids)


def _live_memberships():
    today = timezone.localdate()
    return Membership.objects.filter(status__in=Membership.ENTRY_STATUSES, expiry_date__gt=today)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def kpis(request):
    """GET /api/admin/analytics/kpis/"""
    now, today = timezone.now(), timezone.localdate()
    live = _live_memberships()
    live_count = live.count()
    # A renewal extends the same membership, so "lapsed" = ended in the last 90 days and still ended.
    lapsed = Membership.objects.filter(expiry_date__range=(today - timedelta(days=90), today)).count()
    visits_30 = CheckIn.objects.filter(check_in_time__gte=now - timedelta(days=30))
    avg_visit = visits_30.filter(duration_minutes__isnull=False).aggregate(a=Avg('duration_minutes'))['a']
    visitors = visits_30.values('member').distinct().count()
    return Response({
        'total_members': User.objects.filter(role='member').count(),
        'active_members': live_count,
        'new_members_30d': User.objects.filter(role='member', created_at__gte=now - timedelta(days=30)).count(),
        'lapsed_90d': lapsed,
        'avg_visit_minutes': round(avg_visit) if avg_visit else None,
        'visits_30d': visits_30.count(),
        'active_visitors_30d': visitors,
        'visit_rate': round(visitors / live_count * 100, 1) if live_count else None,
    })


@api_view(['GET'])
@permission_classes([IsAdminUser])
def growth(request):
    """GET /api/admin/analytics/growth/ → last 12 months: new members and memberships that ended."""
    first = timezone.localdate().replace(day=1) - relativedelta(months=11)
    joined = {r['m'].strftime('%Y-%m'): r['n'] for r in
              User.objects.filter(role='member', created_at__date__gte=first)
              .annotate(m=TruncMonth('created_at')).values('m').annotate(n=Count('id')) if r['m']}
    ended = {r['m'].strftime('%Y-%m'): r['n'] for r in
             Membership.objects.filter(expiry_date__gte=first, expiry_date__lte=timezone.localdate())
             .annotate(m=TruncMonth('expiry_date')).values('m').annotate(n=Count('id')) if r['m']}
    out = []
    for i in range(12):
        d = first + relativedelta(months=i)
        k = d.strftime('%Y-%m')
        out.append({'month': d.strftime('%b %Y'), 'short': d.strftime('%b'),
                    'new_members': joined.get(k, 0), 'ended': ended.get(k, 0)})
    return Response(out)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def insights(request):
    """
    GET /api/admin/analytics/insights/ → [{priority, icon, title, description, action, path, ids}]
    `ids` are the records an item is about; `path` carries them as ?highlight= so that page marks them.
    """
    today, now = timezone.localdate(), timezone.now()
    out = []

    def add(priority, icon, title, description, action, path, ids):
        ids = [int(i) for i in ids]
        out.append({'priority': priority, 'icon': icon, 'title': title, 'description': description,
                    'action': action, 'path': with_highlight(path, ids), 'ids': ids})

    ids = list(Reg.objects.filter(status=Reg.STATUS_PAID).values_list('id', flat=True))
    if n := len(ids):
        add('high', 'how_to_reg', f'{n} paid signup{"s" if n != 1 else ""} waiting',
            'They have paid but can’t enter the gym until approved.', 'Review registrations', '/admin/registrations', ids)

    ids = list(PaymentIntent.objects.filter(status='review').values_list('id', flat=True))
    if n := len(ids):
        add('high', 'report', f'{n} payment{"s" if n != 1 else ""} need attention',
            'Money was taken but the purchase wasn’t delivered — deliver it or refund.', 'Open shop orders',
            '/admin/shop-orders?tab=review', ids)

    ids = list(_live_memberships().filter(expiry_date__lte=today + timedelta(days=7)).values_list('member_id', flat=True))
    if n := len(ids):
        add('high', 'hourglass_bottom', f'{n} membership{"s" if n != 1 else ""} end this week',
            'A reminder now is the cheapest way to keep them.', 'See members', '/admin/members', ids)

    last = dict(CheckIn.objects.values('member').annotate(last=Max('check_in_time')).values_list('member', 'last'))
    cutoff = now - timedelta(days=AT_RISK_DAYS)
    live = _live_memberships().filter(start_date__lte=today - timedelta(days=AT_RISK_DAYS))
    ids = [mid for mid in live.values_list('member_id', flat=True) if not last.get(mid) or last[mid] < cutoff]
    if at_risk := len(ids):
        add('medium', 'person_off', f'{at_risk} paying member{"s" if at_risk != 1 else ""} haven’t visited in {AT_RISK_DAYS}+ days',
            'Members who stop coming rarely renew. Send them a message from the members page.', 'See members', '/admin/members', ids)

    ids = list(Product.objects.filter(is_active=True, stock_quantity__lte=5).values_list('id', flat=True))
    if n := len(ids):
        add('medium', 'inventory_2', f'{n} shop product{"s" if n != 1 else ""} almost sold out',
            'Five or fewer left in stock.', 'Open shop products', '/admin/shop', ids)

    full = [c for c in GymClass.objects.filter(is_active=True).annotate(
        w=Count('enrollments', filter=Q(enrollments__status='waitlist'))) if c.w]
    if full:
        add('medium', 'groups', f'{len(full)} class{"es" if len(full) != 1 else ""} with a waitlist',
            f'{sum(c.w for c in full)} members are waiting — add spots or another session.', 'Open classes', '/admin/classes', [c.id for c in full])

    ids = list(Exercise.objects.filter(is_active=True).filter(Q(qr_tag_id='') | Q(qr_tag_id__isnull=True))
               .values_list('id', flat=True))
    if n := len(ids):
        add('low', 'qr_code_2', f'{n} machine{"s" if n != 1 else ""} without a QR code',
            'Members can’t scan them to log sets.', 'Open machines', '/admin/machines', ids)

    ids = list(TrainerProfile.objects.filter(user__is_active=True, is_verified=False).values_list('id', flat=True))
    if n := len(ids):
        add('low', 'verified', f'{n} trainer{"s" if n != 1 else ""} not verified',
            'Members see a verified badge on checked trainers.', 'Open trainers', '/admin/trainers', ids)

    return Response(out)
