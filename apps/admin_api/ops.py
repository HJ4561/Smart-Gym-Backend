"""
Back office for member purchases: facility pricing, shop orders, payments that
need review, trainer booking hours and sessions. Role admin or manager.
"""
from datetime import date, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.catalog.models import TrainerAvailability, TrainerProfile, TrainerSession
from apps.members.models import FacilityAddon, MembershipFacility
from apps.notifications.service import notify
from apps.payments import gateway
from apps.payments.models import PaymentIntent
from apps.public_api.moyasar import MoyasarError, MoyasarUnavailable
from apps.shop.models import Order, Product
from apps.shop.serializers import OrderDetailSerializer
from apps.site_content.models import SiteItem

from .permissions import IsAdminUser


def _title(item):
    return ((item.data or {}).get('title') or {}).get('en') or f'Facility #{item.id}'


# ============================================================
# FACILITY PRICING
# ============================================================
STATUS_PRESETS = {   # quick status from the admin Facilities page (text shown on the website)
    'ok': {'en': 'OPEN', 'ar': 'مفتوح'},
    'busy': {'en': 'BUSY', 'ar': 'مزدحم'},
    'closed': {'en': 'CLOSED', 'ar': 'مغلق'},
}


def _facility_row(item, addon, members):
    d = item.data or {}
    return {
        'id': item.id,
        'name': _title(item),
        'category': d.get('category', ''),
        'location': (d.get('location') or {}).get('en', ''),
        'status_text': (d.get('status') or {}).get('en', ''),
        'status_tone': d.get('status_tone', 'ok'),
        'image_url': item.image_url,
        'is_active': item.is_active,
        'monthly_price': str(addon.monthly_price) if addon else '0.00',
        'is_bookable': bool(addon and addon.is_bookable),
        'capacity': addon.capacity if addon else 0,
        'active_members': members,
    }


@api_view(['GET'])
@permission_classes([IsAdminUser])
def facility_pricing(request):
    """
    GET /api/admin/facility-pricing/ — the facilities of the public website (the same ones members
    add to their plan), with price, bookable, capacity and members who have access, plus totals.
    """
    from django.db.models import Sum
    items = SiteItem.objects.filter(collection='facilities').order_by('order', 'id')
    addons = {a.item_id: a for a in FacilityAddon.objects.filter(item__in=items)}
    active = MembershipFacility.objects.filter(expires_on__gt=date.today(), item__in=items)
    counts = dict(active.values_list('item_id').annotate(n=Count('id')))
    since = timezone.now() - timedelta(days=30)
    rows = [_facility_row(i, addons.get(i.id), counts.get(i.id, 0)) for i in items]
    return Response({
        'summary': {
            'total': len(rows),
            'on_website': sum(1 for r in rows if r['is_active']),
            'bookable': sum(1 for r in rows if r['is_bookable']),
            'members_with_addons': active.values('member').distinct().count(),
            'addon_revenue_30d': str(MembershipFacility.objects.filter(created_at__gte=since)
                                     .aggregate(s=Sum('amount'))['s'] or Decimal('0.00')),
        },
        'results': rows,
    })


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def facility_pricing_update(request, item_id):
    """PATCH /api/admin/facility-pricing/<item_id>/ { monthly_price, is_bookable }"""
    item = get_object_or_404(SiteItem, id=item_id, collection='facilities')
    addon, _ = FacilityAddon.objects.get_or_create(item=item)
    if 'monthly_price' in request.data:
        try:
            price = Decimal(str(request.data['monthly_price'])).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError):
            return Response({'monthly_price': 'Enter a price in SAR.'}, status=400)
        if price < 0 or price > 100000:
            return Response({'monthly_price': 'Price must be between 0 and 100,000.'}, status=400)
        addon.monthly_price = price
    if 'is_bookable' in request.data:
        addon.is_bookable = bool(request.data['is_bookable'])
    if addon.is_bookable and addon.monthly_price <= 0:
        return Response({'monthly_price': 'Set a price above 0 before making it bookable.'}, status=400)
    if 'capacity' in request.data:
        try:
            addon.capacity = max(0, int(request.data['capacity'] or 0))
        except (TypeError, ValueError):
            return Response({'capacity': 'Enter a whole number.'}, status=400)
    addon.save()

    # Website-side switches live on the list item itself.
    changed = []
    if 'is_active' in request.data:
        item.is_active = bool(request.data['is_active'])
        changed.append('is_active')
    tone = request.data.get('status_tone')
    if tone:
        if tone not in STATUS_PRESETS:
            return Response({'status_tone': 'Use ok, busy or closed.'}, status=400)
        item.data = {**(item.data or {}), 'status_tone': tone, 'status': STATUS_PRESETS[tone]}
        changed.append('data')
    if changed:
        item.updated_by = request.user
        item.save(update_fields=changed + ['updated_by', 'updated_at'])
    members = MembershipFacility.objects.filter(item=item, expires_on__gt=date.today()).count()
    return Response(_facility_row(item, addon, members))


# ============================================================
# SHOP ORDERS
# ============================================================
# Allowed staff moves. 'pending' orders are cancelled by the member or here.
ORDER_FLOW = {
    'pending': ['cancelled'],
    'paid': ['preparing', 'ready', 'dispatched'],
    'preparing': ['ready', 'dispatched'],
    'ready': ['completed'],
    'dispatched': ['completed'],
}
ORDER_NOTIFY = {
    'preparing': ('Order being prepared', 'We are preparing order {n}.'),
    'ready': ('Order ready for pickup', 'Order {n} is ready at the front desk.'),
    'dispatched': ('Order in your locker', 'Order {n} is in locker {locker}.'),
    'completed': ('Order collected', 'Thanks — order {n} is complete.'),
}


def _paid_intents(order):
    return order.payment_intents.filter(status__in=['paid', 'review'])


def _order_row(o):
    intent = o.payment_intents.filter(status__in=['paid', 'review', 'refunded']).order_by('-created_at').first()
    return {
        'order_number': o.order_number,
        'status': o.status,
        'member': {'id': o.member_id, 'name': o.member.full_name, 'email': o.member.email},
        'delivery_method': o.delivery_method,
        'locker_number': o.locker_number,
        'total': str(o.total),
        'item_count': sum(i.quantity for i in o.items.all()),
        'paid_with': (intent.method if intent else None),
        'payment_status': (intent.status if intent else None),
        'created_at': o.created_at,
        'next': ORDER_FLOW.get(o.status, []),
        'refundable': o.status in ('paid', 'preparing', 'ready', 'dispatched', 'completed')
                      and _paid_intents(o).exists(),
    }


@api_view(['GET'])
@permission_classes([IsAdminUser])
def shop_orders(request):
    """
    GET /api/admin/shop/orders/?status=&q=  (status 'open' = paid/preparing/ready/dispatched)
        &sort=created_at|total|status|order_number (−desc)  &filter_status= &filter_delivery= &filter_member=
        &filter_placed_after= &filter_placed_before=
    """
    from apps.core import listing
    from apps.shop.services import expire_stale_orders
    expire_stale_orders()
    qs = listing.apply(
        Order.objects.select_related('member').prefetch_related('items', 'payment_intents'), request.query_params,
        default_sort='-created_at',
        sorts={'created_at': 'created_at', 'total': 'total', 'status': 'status', 'order_number': 'order_number'},
        filters={'status': ('status', 'str'), 'delivery': ('delivery_method', 'str'), 'member': ('member_id', 'int'),
                 'placed_after': ('created_at__date__gte', 'date'), 'placed_before': ('created_at__date__lte', 'date')})
    st = request.query_params.get('status', '')
    if st == 'open':
        qs = qs.filter(status__in=['paid', 'preparing', 'ready', 'dispatched'])
    elif st:
        qs = qs.filter(status=st)
    q = request.query_params.get('q', '').strip()
    if q:
        qs = qs.filter(Q(order_number__icontains=q) | Q(member__email__icontains=q)
                       | Q(member__first_name__icontains=q) | Q(member__last_name__icontains=q))
    counts = dict(Order.objects.values_list('status').annotate(n=Count('id')))
    return Response({'counts': counts, 'results': [_order_row(o) for o in qs[:200]]})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def shop_order_detail(request, order_number):
    o = get_object_or_404(Order.objects.select_related('member'), order_number=order_number)
    return Response({**_order_row(o), 'detail': OrderDetailSerializer(o).data})


@api_view(['POST'])
@permission_classes([IsAdminUser])
def shop_order_status(request, order_number):
    """POST /api/admin/shop/orders/<n>/status/ { status }"""
    new = request.data.get('status')
    with transaction.atomic():
        o = get_object_or_404(Order.objects.select_for_update().select_related('member'), order_number=order_number)
        if new not in ORDER_FLOW.get(o.status, []):
            return Response({'detail': f'Can’t move an order from {o.status} to {new}.'}, status=400)
        if new == 'cancelled':
            _restock(o)
        o.status = new
        now = timezone.now()
        if new in ('ready', 'dispatched'):
            o.ready_at = now
        if new == 'completed':
            o.completed_at = now
        o.save()
    if new in ORDER_NOTIFY:
        title, msg = ORDER_NOTIFY[new]
        notify(o.member, 'shop', title, msg.format(n=o.order_number, locker=o.locker_number or ''),
               icon='shopping_bag', action_label='My orders', action_path='/dashboard/shop?tab=orders')
    return Response(_order_row(o))


def _restock(order):
    for item in order.items.all():
        if item.product_id:
            Product.objects.filter(id=item.product_id).update(
                stock_quantity=F('stock_quantity') + item.quantity, total_sold=F('total_sold') - item.quantity)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def shop_order_refund(request, order_number):
    """
    POST /api/admin/shop/orders/<n>/refund/ { reason?, restock? }
    Refunds the order's payment (card → Moyasar refund, credit → locker credit).
    """
    o = get_object_or_404(Order, order_number=order_number)
    intents = list(_paid_intents(o))
    if not intents:
        return Response({'detail': 'This order has no payment to refund.'}, status=400)
    try:
        for intent in intents:
            gateway.refund_intent(intent, request.data.get('reason', ''), request.user)
    except MoyasarUnavailable as exc:
        return Response({'detail': str(exc)}, status=503)
    except (MoyasarError, gateway.PaymentError) as exc:
        return Response({'detail': str(exc)}, status=400)
    with transaction.atomic():
        o = Order.objects.select_for_update().get(id=o.id)
        if request.data.get('restock', o.status != 'completed'):
            _restock(o)
        o.status = 'refunded'
        o.save(update_fields=['status'])
    return Response(_order_row(o))


# ============================================================
# PAYMENTS NEEDING REVIEW
# ============================================================
def _intent_row(i):
    return {
        'id': i.id, 'purpose': i.purpose, 'status': i.status, 'method': i.method,
        'amount': str(i.amount), 'description': i.description, 'failure_reason': i.failure_reason,
        'moyasar_payment_id': i.moyasar_payment_id,
        'member': {'id': i.member_id, 'name': i.member.full_name, 'email': i.member.email},
        'created_at': i.created_at, 'paid_at': i.paid_at,
    }


@api_view(['GET'])
@permission_classes([IsAdminUser])
def payments_review(request):
    """GET /api/admin/payments/review/ — money taken but not delivered (status 'review')."""
    qs = PaymentIntent.objects.select_related('member').filter(status='review').order_by('-created_at')
    return Response([_intent_row(i) for i in qs[:200]])


@api_view(['POST'])
@permission_classes([IsAdminUser])
def payment_refund(request, intent_id):
    """POST /api/admin/payments/intents/<id>/refund/ { reason? }"""
    intent = get_object_or_404(PaymentIntent.objects.select_related('member'), id=intent_id)
    try:
        intent = gateway.refund_intent(intent, request.data.get('reason', ''), request.user)
    except MoyasarUnavailable as exc:
        return Response({'detail': str(exc)}, status=503)
    except (MoyasarError, gateway.PaymentError) as exc:
        return Response({'detail': str(exc)}, status=400)
    return Response(_intent_row(intent))


# ============================================================
# TRAINER HOURS + SESSIONS
# ============================================================
def _trainer_row(t):
    return {
        'id': t.id,
        'name': t.user.full_name,
        'title': t.title,
        'photo_url': t.photo_url,
        'hourly_rate': str(t.hourly_rate),
        'is_accepting_clients': t.is_accepting_clients,
        'offers_1on1': t.offers_1on1,
        'windows': [{'id': w.id, 'day_of_week': w.day_of_week, 'start_time': w.start_time.strftime('%H:%M'),
                     'end_time': w.end_time.strftime('%H:%M')}
                    for w in t.availability.filter(is_active=True).order_by('day_of_week', 'start_time')],
        'upcoming_sessions': t.sessions.filter(status='scheduled', scheduled_at__gte=timezone.now()).count(),
    }


@api_view(['GET'])
@permission_classes([IsAdminUser])
def trainer_hours(request):
    """GET /api/admin/trainer-hours/ — trainers with their weekly booking hours."""
    return Response([_trainer_row(t) for t in TrainerProfile.objects.select_related('user').order_by('user__first_name')])


def _parse_time(v):
    try:
        h, m = str(v).split(':')[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return None


@api_view(['PUT'])
@permission_classes([IsAdminUser])
def trainer_hours_update(request, trainer_id):
    """
    PUT /api/admin/trainer-hours/<id>/
    { hourly_rate, is_accepting_clients, offers_1on1,
      windows: [{day_of_week: 0-6 (Mon=0), start_time: "09:00", end_time: "13:00"}] }
    Replaces the trainer's weekly hours. Already-booked sessions are kept.
    """
    t = get_object_or_404(TrainerProfile.objects.select_related('user'), id=trainer_id)
    errors = save_hours(t, request.data, errors={}, allow_rate=True)
    if errors:
        return Response(errors, status=400)
    return Response(_trainer_row(t))


def save_hours(t, data, errors, allow_rate):
    """Validates and saves weekly hours (+ booking flags, + the rate for staff). Returns the errors."""
    windows = []
    for n, w in enumerate(data.get('windows', []) or []):
        try:
            day = int(w.get('day_of_week'))
        except (TypeError, ValueError):
            day = -1
        start, end = _parse_time(w.get('start_time')), _parse_time(w.get('end_time'))
        if not (0 <= day <= 6) or not start or not end:
            errors[f'windows.{n}'] = 'Pick a day and a start and end time.'
        elif end <= start:
            errors[f'windows.{n}'] = 'End time must be after start time.'
        else:
            windows.append((day, start, end))
    for d in range(7):
        day_w = sorted((s, e) for dd, s, e in windows if dd == d)
        if any(day_w[i][1] > day_w[i + 1][0] for i in range(len(day_w) - 1)):
            errors[f'day.{d}'] = 'Hours overlap on the same day.'
    rate = None
    if allow_rate and 'hourly_rate' in data:
        try:
            rate = Decimal(str(data['hourly_rate'])).quantize(Decimal('0.01'))
            if rate < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors['hourly_rate'] = 'Enter a rate in SAR.'
    if errors:
        return errors

    with transaction.atomic():
        if rate is not None:
            t.hourly_rate = rate
        for flag in ('is_accepting_clients', 'offers_1on1'):
            if flag in data:
                setattr(t, flag, bool(data[flag]))
        t.save()
        if 'windows' in data:
            t.availability.all().delete()
            TrainerAvailability.objects.bulk_create(
                TrainerAvailability(trainer=t, day_of_week=d, start_time=s, end_time=e) for d, s, e in windows)
    return errors


def _session_row(s):
    return {
        'id': s.id, 'status': s.status, 'scheduled_at': s.scheduled_at, 'duration_minutes': s.duration_minutes,
        'trainer': {'id': s.trainer_id, 'name': s.trainer.user.full_name},
        'member': {'id': s.member_id, 'name': s.member.full_name, 'email': s.member.email},
        'focus_area': s.focus_area, 'notes': s.notes, 'amount_charged': str(s.amount_charged),
    }


@api_view(['GET'])
@permission_classes([IsAdminUser])
def trainer_sessions(request):
    """GET /api/admin/trainer-sessions/?trainer=<id>&scope=upcoming|past"""
    qs = TrainerSession.objects.select_related('trainer__user', 'member').exclude(status='pending_payment')
    if request.query_params.get('trainer'):
        qs = qs.filter(trainer_id=request.query_params['trainer'])
    now = timezone.now()
    if request.query_params.get('scope', 'upcoming') == 'past':
        qs = qs.filter(scheduled_at__lt=now).order_by('-scheduled_at')
    else:
        qs = qs.filter(scheduled_at__gte=now - timedelta(hours=2)).order_by('scheduled_at')
    return Response([_session_row(s) for s in qs[:200]])


@api_view(['POST'])
@permission_classes([IsAdminUser])
def trainer_session_status(request, session_id):
    """
    POST /api/admin/trainer-sessions/<id>/status/ { status: completed | no_show | cancelled }
    Staff cancelling a paid session refunds it to the member's locker credit.
    """
    row, error = change_session_status(session_id, request.data.get('status'), by='the gym')
    return Response(error, status=400) if error else Response(row)


def change_session_status(session_id, new, by, trainer_id=None):
    """
    (row, None) or (None, {detail}). Completed / no-show / cancelled; a cancelled paid session is
    refunded to the member's locker credit. `trainer_id` limits it to that coach's own sessions.
    """
    if new not in ('completed', 'no_show', 'cancelled'):
        return None, {'detail': 'Status must be completed, no_show or cancelled.'}
    with transaction.atomic():
        qs = TrainerSession.objects.select_for_update().select_related('trainer__user', 'member')
        if trainer_id is not None:
            qs = qs.filter(trainer_id=trainer_id)
        s = get_object_or_404(qs, id=session_id)
        if s.status != 'scheduled':
            return None, {'detail': f'This session is already {s.status}.'}
        if new in ('completed', 'no_show') and s.scheduled_at > timezone.now():
            return None, {'detail': 'This session hasn’t started yet — mark it after it begins.'}
        s.status = new
        s.save(update_fields=['status'])
        if new == 'cancelled' and s.amount_charged:
            from apps.payments.models import LockerCredit
            credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=s.member)
            LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') + s.amount_charged)
            gateway.record_transaction(s.member, f'Refund — session #{s.id} cancelled by {by}', 'refund',
                                       -s.amount_charged, 'Locker credit', f'session:{s.id}')
    if new == 'cancelled':
        when = timezone.localtime(s.scheduled_at).strftime('%d %b %H:%M')
        notify(s.member, 'trainer', f'Session cancelled by {by}',
               f'Your session with {s.trainer.user.full_name} on {when} was cancelled'
               + (f'; SAR {s.amount_charged} is back in your locker credit.' if s.amount_charged else '.'),
               icon='event_busy', priority='high', action_label='Book again',
               action_path=f'/dashboard/trainers/{s.trainer_id}')
    return _session_row(s), None
