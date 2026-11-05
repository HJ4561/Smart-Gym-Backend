from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db import transaction as db_transaction
from decimal import Decimal
import uuid
from django.utils import timezone

from django.conf import settings
from django.shortcuts import get_object_or_404

from apps.public_api.moyasar import MoyasarError, MoyasarUnavailable

from . import gateway
from .credit import enabled as credit_enabled
from .models import PaymentMethod, Transaction, LockerCredit, Invoice, PaymentIntent
from .serializers import (
    PaymentMethodSerializer,
    TransactionListSerializer,
    TransactionSerializer,
    LockerCreditSerializer,
    InvoiceSerializer,
)


# ============================================
# PAYMENT METHODS
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def payment_methods(request):
    """
    GET /api/payments/methods/ → how the member has paid recently.
    Card details are entered on Moyasar's hosted form and never stored here,
    so there is no "add card" endpoint.
    """
    labels = (Transaction.objects.filter(member=request.user, status='settled')
              .exclude(payment_method_label='').order_by('-created_at')
              .values_list('payment_method_label', flat=True)[:50])
    seen = []
    for label in labels:
        if label not in seen:
            seen.append(label)
    return Response({
        'recent': seen[:3],
        'accepted': ['mada', 'Visa', 'Mastercard', 'STC Pay'] + (['Locker credit'] if credit_enabled() else []),
    })


# ============================================
# TRANSACTIONS
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_transactions(request):
    """
    GET /api/payments/transactions/?status=settled
    """
    qs = Transaction.objects.filter(member=request.user)

    status_filter = request.query_params.get('status')
    if status_filter:
        qs = qs.filter(status=status_filter)

    type_filter = request.query_params.get('type')
    if type_filter:
        qs = qs.filter(transaction_type=type_filter)

    return Response(TransactionListSerializer(qs[:100], many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def transaction_detail(request, transaction_id):
    """
    GET /api/payments/transactions/<transaction_id>/
    """
    try:
        txn = Transaction.objects.get(transaction_id=transaction_id, member=request.user)
    except Transaction.DoesNotExist:
        return Response({'detail': 'Transaction not found.'}, status=status.HTTP_404_NOT_FOUND)

    return Response(TransactionSerializer(txn).data)


# ============================================
# LOCKER CREDIT
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_locker_credit(request):
    """
    GET /api/payments/locker-credit/
    """
    credit, _ = LockerCredit.objects.get_or_create(member=request.user)
    return Response({**LockerCreditSerializer(credit).data, 'enabled': credit_enabled()})


# ============================================
# INVOICES
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_invoices(request):
    """
    GET /api/payments/invoices/
    """
    qs = Invoice.objects.filter(member=request.user)[:50]
    return Response(InvoiceSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def invoice_detail(request, invoice_number):
    """
    GET /api/payments/invoices/<invoice_number>/
    """
    try:
        invoice = Invoice.objects.get(invoice_number=invoice_number, member=request.user)
    except Invoice.DoesNotExist:
        return Response({'detail': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)

    return Response(InvoiceSerializer(invoice).data)


# ============================================
# PAYMENT SUMMARY (for /payments page)
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def payment_summary(request):
    """
    GET /api/payments/summary/
    Returns KPIs for the payments page dashboard.
    """
    user = request.user
    transactions = Transaction.objects.filter(member=user)

    settled = transactions.filter(status='settled')
    pending = transactions.filter(status='pending')
    refunded = transactions.filter(status='refunded')

    total_spent = sum(t.amount for t in settled if t.amount > 0)
    pending_amount = sum(t.amount for t in pending)
    refunded_amount = sum(abs(t.amount) for t in refunded)

    credit, _ = LockerCredit.objects.get_or_create(member=user)

    return Response({
        'total_transactions': transactions.count(),
        'total_spent': float(total_spent),
        'pending_amount': float(pending_amount),
        'refunded_amount': float(refunded_amount),
        'locker_credit': LockerCreditSerializer(credit).data,
        'locker_credit_enabled': credit_enabled(),          # off: top-ups and paying with credit are "coming soon"
        'pending_invoices': Invoice.objects.filter(member=user, status='pending').count(),
    })


# ============================================
# CHECKOUT (Moyasar / locker credit) — see gateway.py
# ============================================
def _intent_payload(intent, with_params=False):
    data = {
        'id': intent.id,
        'purpose': intent.purpose,
        'status': intent.status,
        'method': intent.method,
        'amount': str(intent.amount),
        'currency': 'SAR',
        'description': intent.description,
        'failure_reason': intent.failure_reason,
        'order_number': intent.order.order_number if intent.order_id else None,
        'session_id': intent.trainer_session_id,
        'course_id': intent.course_id,
        'facility_id': intent.facility_item_id,
        'paid_at': intent.paid_at,
    }
    if with_params:
        data['payment'] = gateway.checkout_params(intent)
    return data


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def checkout(request):
    """
    POST /api/payments/checkout/
    Body: { "purpose": "locker_topup", "amount": 50 }
        | { "purpose": "shop_order", "order_number": "ORD-..." }
        | { "purpose": "trainer_session", "session_id": 12 }
        | { "purpose": "course", "course_id": 3 }
        | { "purpose": "renewal", "plan_id": 2, "months": 3 }
        | { "purpose": "video", "video_id": 7 }
        | { "purpose": "class_pass", "class_id": 4, "kind": "full" | "session", "date": "2026-11-03" }
        + optional "method": "locker_credit" to pay from the balance right away.
        + optional "business_customer_id": a company the member saved → a standard (B2B) tax invoice.
    Returns the intent and, for card payments, the Moyasar form parameters.
    """
    try:
        intent = gateway.create_intent(request.user, request.data.get('purpose'), request.data)
        if request.data.get('method') == 'locker_credit':
            intent = gateway.pay_with_credit(intent)
            return Response(_intent_payload(intent), status=status.HTTP_201_CREATED)
    except gateway.PaymentError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if not settings.MOYASAR_PUBLISHABLE_KEY:
        return Response({'detail': 'Online payments are not configured yet.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(_intent_payload(intent, with_params=True), status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def intent_detail(request, intent_id):
    """GET /api/payments/intents/<id>/"""
    intent = get_object_or_404(PaymentIntent, id=intent_id, member=request.user)
    return Response(_intent_payload(intent))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def intent_attach(request, intent_id):
    """POST /api/payments/intents/<id>/attach/ { "payment_id": "..." } — from the form's on_completed."""
    intent = get_object_or_404(PaymentIntent, id=intent_id, member=request.user)
    return _sync(lambda: gateway.attach_payment(intent, request.data.get('payment_id')))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def intent_verify(request):
    """POST /api/payments/intents/verify/ { "payment_id": "..." } — the callback page after 3-D Secure."""
    payment_id = request.data.get('payment_id')
    if not isinstance(payment_id, str) or not payment_id or len(payment_id) > 64:
        return Response({'detail': 'Invalid payment id.'}, status=status.HTTP_400_BAD_REQUEST)

    def run():
        intent = gateway.sync_intent(payment_id)
        if intent.member_id != request.user.id:
            raise MoyasarError('This payment belongs to another account.')
        return intent
    return _sync(run)


def _sync(fn):
    try:
        intent = fn()
    except MoyasarUnavailable as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    except MoyasarError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_intent_payload(intent))


# ============================================
# RECEIPTS (signed link — opens from an email without signing in)
# ============================================
from django.http import Http404, HttpResponse  # noqa: E402
from rest_framework.decorators import authentication_classes  # noqa: E402
from rest_framework.permissions import AllowAny  # noqa: E402


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def receipt_page(request, token):
    """GET /api/payments/receipts/<token>/ → bilingual printable receipt (HTML)."""
    from apps.members.models import MembershipRegistration
    from .models import Transaction
    from .receipts import from_registration, from_transaction, read_token, render_html
    kind, pk = read_token(token)
    if kind == 't':
        txn = Transaction.objects.select_related('member').filter(id=pk, amount__gt=0).first()
        data = from_transaction(txn) if txn else None
    elif kind == 'z':
        from apps.zatca.models import ZatcaDocument
        from .receipts import from_document
        doc = ZatcaDocument.objects.select_related('device', 'original__transaction__member', 'transaction',
                                                   'registration').filter(id=pk).first()
        data = from_document(doc) if doc else None
    elif kind == 'r':
        reg = MembershipRegistration.objects.select_related('plan').filter(id=pk, payment_status__in=['paid', 'refunded']).first()
        data = from_registration(reg) if reg else None
    else:
        data = None
    if data is None:
        raise Http404('Receipt not found.')
    resp = HttpResponse(render_html(data), content_type='text/html; charset=utf-8')
    resp['X-Robots-Tag'] = 'noindex'
    resp['Cache-Control'] = 'private, max-age=300'
    return resp
