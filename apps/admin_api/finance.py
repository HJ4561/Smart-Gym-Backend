"""
Admin → Finance. Every figure comes from recorded transactions:
  received  = settled card / STC Pay / desk payments and top-ups, minus refunds to cards
              (spending or refunding locker credit is not new money — see cash_transactions)
"""
from datetime import date, datetime, time, timedelta

from dateutil.relativedelta import relativedelta
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.payments.models import Invoice, PaymentIntent, Transaction

from apps.payments.receipts import receipt_url

from .permissions import IsAdminUser
from .views import _range, cash_transactions

STREAMS = {   # transaction_type → (label, icon, colour)
    'subscription': ('Memberships & add-ons', 'card_membership', 'bg-primary-container'),
    'retail': ('Shop', 'shopping_bag', 'bg-emerald-400'),
    'coaching': ('Trainers & courses', 'school', 'bg-primary'),
    'topup': ('Locker credit top-ups', 'account_balance_wallet', 'bg-amber-400'),
    'refund': ('Refunds', 'undo', 'bg-error'),
}


def _f(v):
    return round(float(v or 0), 2)


def _day(request):
    """(start, end, 1, label) for ?date=YYYY-MM-DD; None without it; a 400 Response for a bad date."""
    raw = (request.query_params.get('date') or '').strip()
    if not raw:
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return Response({'date': ['Use the date format YYYY-MM-DD.']}, status=400)
    if day > timezone.localdate():
        return Response({'date': ['Choose today or an earlier day.']}, status=400)
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1), 1, day.isoformat()


@api_view(['GET'])
@permission_classes([IsAdminUser])
def finance_overview(request):
    """
    GET /api/admin/finance/overview/?range=1d|7d|30d|90d|ytd  or  ?date=YYYY-MM-DD (that one day, gym time)
    → received, run rate, refunds, payments to review. "This month" and the review queue ignore the filter.
    """
    window = _day(request)
    if isinstance(window, Response):
        return window
    if window:
        since, until, days, label = window
    else:
        (since, days, label), until = _range(request), None
    period = Transaction.objects.filter(created_at__gte=since)
    if until is not None:
        period = period.filter(created_at__lt=until)
    cash = cash_transactions(period)
    received = cash.aggregate(t=Sum('amount'))['t'] or 0
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month = cash_transactions(Transaction.objects.filter(created_at__gte=month_start)).aggregate(t=Sum('amount'))['t'] or 0
    refunds = period.filter(transaction_type='refund').aggregate(t=Sum('amount'))['t'] or 0
    review = PaymentIntent.objects.filter(status='review')
    return Response({
        'range_label': label,
        'from': since.isoformat(), 'to': (until or timezone.now()).isoformat(),
        'received': _f(received),
        'this_month': _f(this_month),
        'run_rate': _f(float(received) / days * 365),
        'refunds': _f(abs(refunds)),
        'payment_count': cash.filter(amount__gt=0).count(),
        'review_count': review.count(),
        'review_amount': _f(review.aggregate(t=Sum('amount'))['t']),
        # names the old page used
        'mrr': _f(this_month), 'arr': _f(float(received) / days * 365),
        'pending_amount': _f(review.aggregate(t=Sum('amount'))['t']), 'pending_count': review.count(),
        'refunds_30d': _f(abs(refunds)),
    })


@api_view(['GET'])
@permission_classes([IsAdminUser])
def revenue_monthly(request):
    """GET /api/admin/finance/revenue-monthly/?months=12 → money received per calendar month (gym time)."""
    try:
        months = max(1, min(int(request.query_params.get('months', 12)), 24))
    except (TypeError, ValueError):
        months = 12
    first = date.today().replace(day=1) - relativedelta(months=months - 1)
    rows = (cash_transactions(Transaction.objects.filter(created_at__date__gte=first))
            .annotate(month=TruncMonth('created_at')).values('month').annotate(revenue=Sum('amount')))
    by_month = {r['month'].strftime('%Y-%m'): float(r['revenue'] or 0) for r in rows if r['month']}
    out = []
    for i in range(months):
        d = first + relativedelta(months=i)
        out.append({'month': d.strftime('%b %Y'), 'short': d.strftime('%b'),
                    'revenue': round(by_month.get(d.strftime('%Y-%m'), 0), 2)})
    return Response(out)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def revenue_streams_view(request):
    """GET /api/admin/finance/revenue-streams/?range= → by type and by actual payment method."""
    since, days, label = _range(request)
    cash = cash_transactions(Transaction.objects.filter(created_at__gte=since))
    gross = float(cash.filter(amount__gt=0).aggregate(t=Sum('amount'))['t'] or 0)
    streams = []
    for row in cash.values('transaction_type').annotate(total=Sum('amount')).order_by('-total'):
        name, icon, colour = STREAMS.get(row['transaction_type'], ('Other', 'receipt_long', 'bg-surface-variant'))
        streams.append({'label': name, 'value': _f(row['total']), 'icon': icon, 'color': colour,
                        'percent': round(abs(float(row['total'])) / gross * 100, 1) if gross else 0})

    def method_of(label):
        l = (label or '').lower()
        if 'stc' in l:
            return 'STC Pay'
        if 'mada' in l:
            return 'mada'
        if 'visa' in l:
            return 'Visa'
        if 'master' in l:
            return 'Mastercard'
        if 'cash' in l:
            return 'Cash at desk'
        if 'terminal' in l:
            return 'Card terminal'
        return label or 'Other'

    methods = {}
    for row in cash.filter(amount__gt=0).values('payment_method_label').annotate(total=Sum('amount')):
        k = method_of(row['payment_method_label'])
        methods[k] = methods.get(k, 0) + float(row['total'])
    payment_methods = [{'label': k, 'value': round(v, 2), 'percent': round(v / gross * 100, 1) if gross else 0,
                        'icon': 'payments' if 'desk' in k.lower() else 'credit_card'}
                       for k, v in sorted(methods.items(), key=lambda x: -x[1])]
    return Response({'range_label': label, 'streams': streams, 'payment_methods': payment_methods, 'total': round(gross, 2)})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def top_members_view(request):
    """GET /api/admin/finance/top-members/?limit=5 → members by money actually paid (all time)."""
    try:
        limit = max(1, min(int(request.query_params.get('limit', 5)), 50))
    except (TypeError, ValueError):
        limit = 5
    rows = (cash_transactions(Transaction.objects.all()).values('member', 'member__first_name', 'member__last_name',
                                                               'member__email', 'member__membership__plan__display_name')
            .annotate(total=Sum('amount')).filter(total__gt=0).order_by('-total')[:limit])
    members = []
    for i, r in enumerate(rows, start=1):
        name = f"{r['member__first_name']} {r['member__last_name']}".strip() or r['member__email']
        members.append({'rank': i, 'id': r['member'], 'name': name,
                        'initials': ''.join(p[0] for p in name.split()[:2]).upper() or '?',
                        'tier': r['member__membership__plan__display_name'] or '—', 'ltv': _f(r['total'])})
    return Response({'members': members, 'total_ltv': round(sum(m['ltv'] for m in members), 2)})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def transactions_list(request):
    """
    GET /api/admin/finance/transactions/?page=&q=&type=&range=
        &sort=created_at|amount|type (−desc)  &filter_type= &filter_status= &filter_member= &filter_after=
        &filter_before= &filter_min_amount= &filter_max_amount=
    """
    from apps.core import listing
    qs = listing.apply(
        Transaction.objects.select_related('member'), request.query_params, default_sort='-created_at',
        sorts={'created_at': 'created_at', 'amount': 'amount', 'type': 'transaction_type'},
        filters={'type': ('transaction_type', 'str'), 'status': ('status', 'str'), 'member': ('member_id', 'int'),
                 'after': ('created_at__date__gte', 'date'), 'before': ('created_at__date__lte', 'date'),
                 'min_amount': ('amount__gte', 'decimal'), 'max_amount': ('amount__lte', 'decimal')})
    if request.query_params.get('range'):
        since, _, _ = _range(request)
        qs = qs.filter(created_at__gte=since)
    if request.query_params.get('type'):
        qs = qs.filter(transaction_type=request.query_params['type'])
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(transaction_id__icontains=q) | Q(description__icontains=q) | Q(member__email__icontains=q)
                       | Q(member__first_name__icontains=q) | Q(member__last_name__icontains=q))
    try:
        page = max(1, int(request.query_params.get('page', 1)))
    except ValueError:
        page = 1
    total = qs.count()
    rows = qs[(page - 1) * 20: page * 20]
    return Response({
        'results': [{
            'id': t.id, 'transaction_id': t.transaction_id, 'member_email': t.member.email,
            'member_name': t.member.full_name, 'description': t.description, 'type': t.transaction_type,
            'amount': _f(t.amount), 'method': t.payment_method_label, 'status': t.status, 'created_at': t.created_at,
            'receipt_url': receipt_url('t', t.id) if t.amount > 0 else None,
        } for t in rows],
        'total': total, 'page': page, 'pages': max(1, (total + 19) // 20),
    })


@api_view(['GET'])
@permission_classes([IsAdminUser])
def transaction_detail(request, transaction_id):
    """GET /api/admin/finance/transactions/<id>/ → transaction + its invoice + the payment behind it."""
    t = get_object_or_404(Transaction.objects.select_related('member'), id=transaction_id)
    inv = Invoice.objects.filter(transaction=t).first()
    intent = PaymentIntent.objects.filter(transaction=t).first()
    return Response({
        'id': t.id, 'transaction_id': t.transaction_id, 'member': t.member.full_name, 'member_email': t.member.email,
        'member_id': t.member.id, 'description': t.description, 'amount': _f(t.amount), 'subtotal': _f(t.subtotal),
        'tax_amount': _f(t.tax_amount), 'status': t.status, 'type': t.get_transaction_type_display(),
        'method': t.payment_method_label or '—', 'gateway_reference': t.gateway_reference, 'created_at': t.created_at,
        'settled_at': t.settled_at,
        'receipt_url': receipt_url('t', t.id) if t.amount > 0 else None,
        'invoice': {'number': inv.invoice_number, 'status': inv.status, 'total': _f(inv.total), 'paid_at': inv.paid_at}
        if inv else None,
        'payment': {'id': intent.id, 'purpose': intent.get_purpose_display(), 'status': intent.status,
                    'moyasar_payment_id': intent.moyasar_payment_id} if intent else None,
    })


@api_view(['POST'])
@permission_classes([IsAdminUser])
def transaction_email_receipt(request, transaction_id):
    """POST /api/admin/finance/transactions/<id>/email-receipt/ → emails the bilingual receipt to the member."""
    from django.conf import settings

    from apps.members.services import CONSOLE_EMAIL_BACKEND
    from apps.payments.receipts import from_transaction, send_receipt_email
    t = get_object_or_404(Transaction.objects.select_related('member'), id=transaction_id)
    if t.amount <= 0:
        return Response({'detail': 'Refunds have no receipt to send.'}, status=400)
    if settings.EMAIL_BACKEND == CONSOLE_EMAIL_BACKEND and not settings.DEBUG:
        return Response({'detail': 'Email isn’t configured (SMTP).'}, status=400)
    send_receipt_email(from_transaction(t))
    return Response({'detail': f'Receipt emailed to {t.member.email}.'})
