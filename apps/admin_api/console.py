"""
Admin console chrome: the header's "needs attention" alerts and global search.
Everything is computed from live data; nothing is stored.
"""
from datetime import date, timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.accounts.models import User
from apps.catalog.models import TrainerSession
from apps.members.models import Membership, MembershipRegistration as Reg
from apps.payments.models import PaymentIntent
from apps.shop.models import Order
from apps.site_content.models import ContactMessage
from apps.workouts.models import Exercise

from .permissions import IsAdminUser


@api_view(['GET'])
@permission_classes([IsAdminUser])
def alerts(request):
    """
    GET /api/admin/alerts/ → [{key, icon, title, message, count, path, priority}]
    Only items that need someone to act; `count` sums to the bell badge.
    """
    today = timezone.localdate()
    now = timezone.now()
    items = []

    from apps.core.i18n import bi, pick

    def add(key, icon, count, title, message, path, priority='normal'):
        if count:
            items.append({'key': key, 'icon': icon, 'count': count, 'title': pick(title),
                          'message': pick(message), 'path': path, 'priority': priority})

    from .insights import with_highlight
    ids = list(Reg.objects.filter(status=Reg.STATUS_PAID).values_list('id', flat=True))
    paid_regs = len(ids)
    add('registrations', 'how_to_reg', paid_regs, bi('Registrations to approve', 'تسجيلات بانتظار الموافقة'),
        bi(f'{paid_regs} paid signup{"s" if paid_regs != 1 else ""} waiting for approval.',
           f'{paid_regs} تسجيل مدفوع بانتظار الموافقة.'),
        with_highlight('/admin/registrations', ids), 'high')

    ids = list(PaymentIntent.objects.filter(status='review').values_list('id', flat=True))
    review = len(ids)
    add('payments', 'report', review, bi('Payments to review', 'دفعات تحتاج مراجعة'),
        bi(f'{review} payment{"s" if review != 1 else ""} taken but not delivered — refund or follow up.',
           f'{review} دفعة مُحصّلة لم تُسلَّم — استرد المبلغ أو تابع.'),
        with_highlight('/admin/shop-orders?tab=review', ids), 'high')

    to_fulfil = Order.objects.filter(status__in=['paid', 'preparing']).count()
    add('orders', 'shopping_bag', to_fulfil, bi('Shop orders to prepare', 'طلبات متجر للتجهيز'),
        bi(f'{to_fulfil} paid order{"s" if to_fulfil != 1 else ""} waiting to be prepared.',
           f'{to_fulfil} طلب مدفوع بانتظار التجهيز.'), '/admin/shop-orders')

    sessions_today = TrainerSession.objects.filter(status='scheduled', scheduled_at__date=today,
                                                   scheduled_at__gte=now - timedelta(hours=1)).count()
    add('sessions', 'event_available', sessions_today, bi('Trainer sessions today', 'جلسات المدربين اليوم'),
        bi(f'{sessions_today} 1:1 session{"s" if sessions_today != 1 else ""} still to run today.',
           f'{sessions_today} جلسة فردية متبقية اليوم.'), '/admin/trainer-hours')

    inbox = ContactMessage.objects.filter(status=ContactMessage.STATUS_NEW).count()
    add('inbox', 'mail', inbox, bi('Website messages', 'رسائل الموقع'),
        bi(f'{inbox} unread message{"s" if inbox != 1 else ""} from the contact form.',
           f'{inbox} رسالة غير مقروءة من نموذج التواصل.'), '/admin/site-inbox')

    from apps.nutrition.models import NutritionPlan
    plans = NutritionPlan.objects.filter(status='active', reviewed_at__isnull=True).count()
    add('nutrition', 'restaurant', plans, bi('Nutrition plans to review', 'خطط تغذية للمراجعة'),
        bi(f'{plans} member{"s" if plans != 1 else ""} asked for a plan and {"are" if plans != 1 else "is"} on starter targets.',
           f'{plans} عضو طلب خطة وما زال على الأهداف المبدئية.'),
        '/admin/nutrition')

    from apps.catalog.models import Course
    pending = Course.objects.filter(status='pending').count()
    add('courses', 'rate_review', pending, bi('Courses to review', 'دورات للمراجعة'),
        bi(f'{pending} course{"s" if pending != 1 else ""} from coaches waiting to be published or sent back.',
           f'{pending} دورة من المدربين بانتظار النشر أو الإعادة.'),
        '/admin/trainers?tab=courses&status=pending', 'high')

    ids = list(Membership.objects.filter(status__in=['active', 'expiring'], expiry_date__gt=today,
                                         expiry_date__lte=today + timedelta(days=7)).values_list('member_id', flat=True))
    expiring = len(ids)
    add('expiring', 'hourglass_bottom', expiring, bi('Memberships ending this week', 'عضويات تنتهي هذا الأسبوع'),
        bi(f'{expiring} member{"s" if expiring != 1 else ""} expire within 7 days.',
           f'{expiring} عضو تنتهي عضويته خلال 7 أيام.'), with_highlight('/admin/members', ids))

    return Response({'total': sum(i['count'] for i in items), 'items': items})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def search(request):
    """GET /api/admin/search/?q= → grouped results (members, registrations, orders, machines), 5 each."""
    q = (request.query_params.get('q') or '').strip()
    if len(q) < 2:
        return Response({'members': [], 'registrations': [], 'orders': [], 'machines': []})

    members = (User.objects.filter(role='member')
               .filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                       | Q(phone__icontains=q) | Q(membership__iqama_number__icontains=q)
                       | Q(membership__pass_id__iexact=q))
               .select_related('membership').distinct()[:5])
    regs = (Reg.objects.exclude(status=Reg.STATUS_APPROVED)
            .filter(Q(full_name__icontains=q) | Q(email__icontains=q) | Q(iqama_number__icontains=q))[:5])
    orders = Order.objects.select_related('member').filter(
        Q(order_number__icontains=q) | Q(member__email__icontains=q))[:5]
    machines = Exercise.objects.filter(Q(name__icontains=q) | Q(qr_tag_id__iexact=q) | Q(machine_id__iexact=q))[:5]

    def iqama(u):
        m = getattr(u, 'membership', None)
        return m.iqama_number if m else ''

    return Response({
        'members': [{'id': u.id, 'title': u.full_name, 'subtitle': u.email,
                     'path': f'/admin/member-lookup?iqama={iqama(u)}' if iqama(u) else '/admin/members'}
                    for u in members],
        'registrations': [{'id': r.id, 'title': r.full_name, 'subtitle': f'{r.email} · {r.get_status_display()}',
                           'path': '/admin/registrations'} for r in regs],
        'orders': [{'id': o.order_number, 'title': o.order_number, 'subtitle': f'{o.member.email} · {o.status}',
                    'path': f'/admin/shop-orders?q={o.order_number}'} for o in orders],
        'machines': [{'id': e.id, 'title': e.name, 'subtitle': e.qr_tag_id or e.category,
                      'path': '/admin/machines'} for e in machines],
    })


# ============================================================
# SIDEBAR ACTIVITY BADGES
# ============================================================
FIRST_LOOK_DAYS = 7   # a section never opened counts the last week


def _activity_counters():
    """section (sidebar path after /admin/) → function(since) returning how many new things happened."""
    from apps.catalog.models import Enrollment
    from apps.classes.models import ClassEnrollment
    from apps.members.models import MembershipFacility
    from apps.nutrition.models import NutritionPlan
    from apps.payments.models import Transaction
    return {
        'registrations': lambda t: Reg.objects.filter(submitted_at__gt=t).exclude(status=Reg.STATUS_PENDING).count(),
        'members': lambda t: User.objects.filter(role='member', created_at__gt=t).count(),
        'shop-orders': lambda t: Order.objects.filter(created_at__gt=t).exclude(status__in=['pending', 'cancelled']).count(),
        'trainers': lambda t: Enrollment.objects.filter(enrolled_at__gt=t).count(),
        'trainer-hours': lambda t: TrainerSession.objects.filter(created_at__gt=t).exclude(status='pending_payment').count(),
        'classes': lambda t: ClassEnrollment.objects.filter(enrolled_at__gt=t).exclude(status='cancelled').count(),
        'nutrition': lambda t: NutritionPlan.objects.filter(created_at__gt=t).exclude(status='pending_payment').count(),
        'finance': lambda t: Transaction.objects.filter(created_at__gt=t).count(),
        'facilities': lambda t: MembershipFacility.objects.filter(created_at__gt=t).count(),
        'site-inbox': lambda t: ContactMessage.objects.filter(created_at__gt=t).count(),
    }


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def activity(request):
    """
    GET  /api/admin/activity/ → {section: new_count} since this admin last opened each section.
    POST /api/admin/activity/ {section} → marks it seen now (badge clears).
    """
    from .models import AdminSeen
    counters = _activity_counters()
    if request.method == 'POST':
        section = str(request.data.get('section') or '')
        if section not in counters:
            return Response({'detail': 'Unknown section.'}, status=400)
        AdminSeen.objects.update_or_create(user=request.user, section=section, defaults={'seen_at': timezone.now()})
        return Response({section: 0})
    seen = dict(AdminSeen.objects.filter(user=request.user).values_list('section', 'seen_at'))
    if not seen:
        # A new admin (or a member just promoted) starts clean: everything before their first
        # look counts as seen, so the bubbles only show what happens from now on.
        now = timezone.now()
        AdminSeen.objects.bulk_create([AdminSeen(user=request.user, section=k, seen_at=now) for k in counters],
                                      ignore_conflicts=True)
        return Response({k: 0 for k in counters})
    default = timezone.now() - timedelta(days=FIRST_LOOK_DAYS)   # a section added after their first look
    return Response({k: fn(seen.get(k, default)) for k, fn in counters.items()})
