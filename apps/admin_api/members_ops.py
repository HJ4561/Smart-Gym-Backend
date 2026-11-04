"""
Admin → Members: list with membership state, member file, and real actions
(extend with a desk payment, freeze/unfreeze without losing paid time,
message, password reset, deactivate/reactivate) plus bulk message/freeze/CSV.
"""
import csv
import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.accounts.models import User
from apps.core.i18n import ar_date, bi
from apps.members.models import CheckIn, Membership, MembershipFacility
from apps.members.serializers import MembershipSerializer
from apps.notifications.service import notify
from apps.payments.models import LockerCredit, Transaction

from .permissions import IsAdminUser

logger = logging.getLogger(__name__)
DESK_METHODS = {'cash': 'Cash at desk', 'card_terminal': 'Card terminal at desk'}


def _status(m, today):
    """active | expiring | expired | frozen | none (for filters and badges)."""
    if m is None:
        return 'none'
    if m.effectively_frozen:
        return 'frozen'
    if m.expiry_date <= today:
        return 'expired'
    if m.expiry_date <= today + timedelta(days=14):
        return 'expiring'
    return 'active'


def _row(u, today):
    m = getattr(u, 'membership', None)
    return {
        'id': u.id, 'email': u.email, 'full_name': u.full_name, 'phone': u.phone or '',
        'role': u.role, 'is_active': u.is_active, 'created_at': u.created_at,
        'plan': m.plan.display_name if m else None,
        'iqama_number': m.iqama_number if m else '',
        'expiry_date': m.expiry_date if m else None,
        'time_remaining': m.time_remaining if m else None,
        'membership_status': _status(m, today),
    }


MEMBER_SORTS = {'created_at': 'created_at', 'name': 'first_name', 'email': 'email',
                'expiry': 'membership__expiry_date', 'plan': 'membership__plan__display_name'}
MEMBER_FILTERS = {'plan': ('membership__plan__name', 'str'), 'active': ('is_active', 'bool'),
                  'joined_after': ('created_at__date__gte', 'date'), 'joined_before': ('created_at__date__lte', 'date'),
                  'expires_before': ('membership__expiry_date__lte', 'date')}


@api_view(['GET'])
@permission_classes([IsAdminUser])
def members_list(request):
    """
    GET /api/admin/members/?search=&status=active|expiring|expired|frozen|none|inactive&page=
        &sort=created_at|name|email|expiry|plan (−desc)  &filter_status= (same as status) &filter_plan=
        &filter_active=  &filter_joined_after= &filter_joined_before= &filter_expires_before=
        &ids=1,2,3   only these members (the "Needs attention" links)
    """
    from apps.core import listing
    today = timezone.localdate()
    qs = listing.apply(User.objects.filter(role='member').select_related('membership__plan'), request.query_params,
                       sorts=MEMBER_SORTS, filters=MEMBER_FILTERS, default_sort='-created_at')
    search = (request.query_params.get('search') or '').strip()
    if search:
        qs = qs.filter(Q(email__icontains=search) | Q(first_name__icontains=search) | Q(last_name__icontains=search)
                       | Q(phone__icontains=search) | Q(membership__iqama_number__icontains=search)
                       | Q(membership__pass_id__iexact=search))
    ids = [int(i) for i in (request.query_params.get('ids') or '').split(',') if i.strip().isdigit()]
    if ids:
        qs = qs.filter(id__in=ids)
    st = request.query_params.get('filter_status') or request.query_params.get('status') or ''
    soon = today + timedelta(days=14)
    filters = {
        'inactive': Q(is_active=False),
        'none': Q(membership__isnull=True),
        'frozen': Q(membership__is_frozen=True, membership__frozen_until__gt=today),
        'expired': Q(membership__expiry_date__lte=today),
        'expiring': Q(membership__expiry_date__gt=today, membership__expiry_date__lte=soon),
        'active': Q(membership__expiry_date__gt=soon) & ~Q(membership__is_frozen=True, membership__frozen_until__gt=today),
    }
    if st in filters:
        qs = qs.filter(filters[st])
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(qs, request)
    return paginator.get_paginated_response([_row(u, today) for u in page])


@api_view(['GET'])
@permission_classes([IsAdminUser])
def member_stats(request):
    """GET /api/admin/members/stats/ — counts for the filter chips."""
    today = timezone.localdate()
    base = User.objects.filter(role='member')
    soon = today + timedelta(days=14)
    frozen = Q(membership__is_frozen=True, membership__frozen_until__gt=today)
    return Response({
        'total': base.count(),
        'active': base.filter(Q(membership__expiry_date__gt=soon) & ~frozen).count(),
        'expiring': base.filter(membership__expiry_date__gt=today, membership__expiry_date__lte=soon).count(),
        'expired': base.filter(membership__expiry_date__lte=today).count(),
        'frozen': base.filter(frozen).count(),
        'none': base.filter(membership__isnull=True).count(),
        'inactive': base.filter(is_active=False).count(),
        'new_this_month': base.filter(created_at__year=today.year, created_at__month=today.month).count(),
    })


@api_view(['GET'])
@permission_classes([IsAdminUser])
def member_detail(request, user_id):
    """GET /api/admin/members/<id>/ — the member file."""
    u = get_object_or_404(User.objects.select_related('membership__plan'), id=user_id)
    m = getattr(u, 'membership', None)
    today = timezone.localdate()
    credit = LockerCredit.objects.filter(member=u).first()
    return Response({
        **_row(u, today),
        'must_change_password': u.must_change_password,
        'two_factor': u.two_factor.method if hasattr(u, 'two_factor') and u.two_factor.enabled else None,
        'membership': MembershipSerializer(m).data if m else None,
        'frozen_until': m.frozen_until if m and m.effectively_frozen else None,
        'facilities': [{'name': f.facility_name, 'expires_on': f.expires_on}
                       for f in MembershipFacility.objects.filter(member=u, expires_on__gt=today)],
        'locker_credit': str(credit.balance) if credit else '0.00',
        'recent_payments': [{'date': t.created_at, 'description': t.description, 'amount': str(t.amount),
                             'method': t.payment_method_label, 'status': t.status}
                            for t in Transaction.objects.filter(member=u).order_by('-created_at')[:8]],
        'recent_visits': [{'check_in': c.check_in_time, 'check_out': c.check_out_time, 'zone': c.zone or c.facility}
                          for c in CheckIn.objects.filter(member=u).order_by('-check_in_time')[:8]],
    })


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def update_member(request, user_id):
    """PATCH /api/admin/members/<id>/update/ { first_name, last_name, phone } (+ role: admins only)"""
    from apps.core.validators import apply_person_fields
    u = get_object_or_404(User, id=user_id)
    errors = {}
    apply_person_fields(u, request.data, errors)
    if errors:
        return Response(errors, status=400)
    if 'role' in request.data and request.data['role'] != u.role:
        if request.user.role != 'admin':
            return Response({'detail': 'Only admins can change roles.'}, status=403)
        if request.data['role'] not in dict(User._meta.get_field('role').choices):
            return Response({'role': 'Unknown role.'}, status=400)
        if u.id == request.user.id:
            return Response({'detail': 'You can’t change your own role.'}, status=400)
        u.role = request.data['role']
    u.save()
    return Response({'detail': 'Saved.', 'id': u.id})


# ============================================================
# ACTIONS
# ============================================================
def _months(data):
    try:
        months = int(data.get('months'))
    except (TypeError, ValueError):
        return None
    return months if 1 <= months <= 24 else None


def _extend(u, m, data, staff):
    months = _months(data)
    if months is None:
        return 'Choose 1–24 months.'
    method = data.get('payment')
    if method not in (*DESK_METHODS, 'complimentary'):
        return 'Choose how it was paid: cash, card terminal or complimentary.'
    amount = Decimal('0')
    if method in DESK_METHODS:
        try:
            amount = Decimal(str(data.get('amount'))).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError):
            return 'Enter the amount received.'
        if amount <= 0:
            return 'Enter the amount received.'
    today = date.today()
    base = m.expiry_date if m.expiry_date > today else today
    if m.expiry_date <= today:
        m.start_date = today
    m.expiry_date = base + relativedelta(months=months)
    m.status = 'active'
    m.lifetime_value = (m.lifetime_value or 0) + amount
    m.save()
    note = (data.get('note') or '').strip()[:120]
    if method in DESK_METHODS:
        from apps.payments.gateway import record_transaction
        from apps.zatca.models import BusinessCustomer
        company = (BusinessCustomer.objects.filter(pk=data['business_customer_id'], owner=u, is_active=True).first()
                   if str(data.get('business_customer_id') or '').isdigit() else None)   # a company tax invoice
        record_transaction(u, f'Membership extension — {months} mo (desk){" — " + note if note else ""}',
                           'subscription', amount, DESK_METHODS[method], f'desk:{staff.id}:{timezone.now():%Y%m%d%H%M%S}',
                           buyer=company)
    logger.info('Membership of %s extended %s mo by %s (%s, SAR %s) %s', u.email, months, staff.email, method, amount, note)
    notify(u, 'payment', bi('Membership extended', 'تم تمديد العضوية'),
           bi(f'Your membership now runs until {m.expiry_date:%d %b %Y}.',
              f'تستمر عضويتك الآن حتى {ar_date(m.expiry_date)}.'),
           icon='card_membership', action_label='View membership', action_path='/dashboard/payments')
    return None


def _freeze(u, m, data, staff):
    try:
        days = int(data.get('days'))
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= 90:
        return 'Freeze for 1–90 days.'
    if m.effectively_frozen:
        return 'Already frozen.'
    today = date.today()
    m.is_frozen = True
    m.frozen_until = today + timedelta(days=days)
    m.expiry_date = max(m.expiry_date, today) + timedelta(days=days)   # paid time isn't lost
    m.status = 'frozen'
    m.save()
    notify(u, 'system', bi('Membership frozen', 'تم تجميد العضوية'),
           bi(f'Your membership is paused until {m.frozen_until:%d %b %Y}; your expiry moved to {m.expiry_date:%d %b %Y}.',
              f'عضويتك متوقفة حتى {ar_date(m.frozen_until)}، وانتقل تاريخ انتهائها إلى {ar_date(m.expiry_date)}.'),
           icon='ac_unit', action_label='View membership', action_path='/dashboard/payments')
    return None


def _unfreeze(u, m, data, staff):
    if not m.effectively_frozen:
        return 'This membership is not frozen.'
    today = date.today()
    unused = (m.frozen_until - today).days
    m.expiry_date = m.expiry_date - timedelta(days=max(0, unused))   # give back only the days not used
    m.is_frozen = False
    m.frozen_until = None
    m.status = 'active'
    m.save()
    notify(u, 'system', bi('Membership unfrozen', 'تم إلغاء تجميد العضوية'),
           bi(f'Welcome back! Your membership runs until {m.expiry_date:%d %b %Y}.',
              f'أهلًا بعودتك! تستمر عضويتك حتى {ar_date(m.expiry_date)}.'),
           icon='sunny', action_label='View membership', action_path='/dashboard/payments')
    return None


def _message(u, data):
    title = (data.get('title') or '').strip()[:120]
    message = (data.get('message') or '').strip()[:2000]
    if not title or not message:
        return 'Write a title and a message.'
    notify(u, 'system', title, message, icon='campaign')
    return None


def _reset_password(u, staff):
    from apps.members.services import CONSOLE_EMAIL_BACKEND, generate_temp_password, member_portal_url
    if settings.EMAIL_BACKEND == CONSOLE_EMAIL_BACKEND and not settings.DEBUG:
        return 'Email isn’t configured (SMTP), so a temporary password can’t be sent.'
    temp = generate_temp_password()
    u.set_password(temp)
    u.must_change_password = True
    u.save(update_fields=['password', 'must_change_password'])
    from apps.accounts.two_factor import forget_devices
    forget_devices(u)                       # new password → trusted browsers ask for a code again
    try:
        send_mail('Smart Gym — your temporary password · كلمة المرور المؤقتة',
                  f"Hi {u.first_name or u.email},\n\nA staff member reset your Smart Gym password.\n\n"
                  f"  Login page:         {member_portal_url()}/login\n  Email:              {u.email}\n"
                  f"  Temporary password: {temp}\n\nYou'll be asked to choose a new password when you log in.\n"
                  f"If you didn't ask for this, contact the front desk.\n— Smart Gym\n\n────────────\n\n"
                  f"مرحبًا {u.first_name or u.email}،\n\nأعاد أحد الموظفين تعيين كلمة مرورك في سمارت جيم.\n\n"
                  f"  صفحة الدخول: {member_portal_url()}/login\n  البريد الإلكتروني: {u.email}\n"
                  f"  كلمة المرور المؤقتة: {temp}\n\nسيُطلب منك اختيار كلمة مرور جديدة عند تسجيل الدخول.\n"
                  f"إذا لم تطلب ذلك، تواصل مع مكتب الاستقبال.\n— سمارت جيم",
                  settings.DEFAULT_FROM_EMAIL, [u.email])
    except Exception:   # noqa: BLE001
        logger.exception('Password reset email to %s failed.', u.email)
        return 'The password was reset but the email failed to send — try again.'
    logger.info('Password of %s reset by %s', u.email, staff.email)
    return None


@api_view(['POST'])
@permission_classes([IsAdminUser])
def member_action(request, user_id):
    """
    POST /api/admin/members/<id>/action/
    { action: extend, months, payment: cash|card_terminal|complimentary, amount?, note? }
    { action: freeze, days } | { action: unfreeze } | { action: message, title, message }
    { action: reset_password } | { action: deactivate } | { action: reactivate }
    { action: reset_2fa }   — turns 2-step verification off (lost phone); they can turn it on again
    """
    u = get_object_or_404(User.objects.select_related('membership__plan'), id=user_id)
    action = request.data.get('action')
    if u.id == request.user.id and action == 'deactivate':
        return Response({'detail': 'You can’t deactivate your own account.'}, status=400)
    with transaction.atomic():
        m = Membership.objects.select_for_update().filter(member=u).first()
        if action in ('extend', 'freeze', 'unfreeze') and m is None:
            return Response({'detail': 'This member has no membership.'}, status=400)
        if action == 'extend':
            err = _extend(u, m, request.data, request.user)
        elif action == 'freeze':
            err = _freeze(u, m, request.data, request.user)
        elif action == 'unfreeze':
            err = _unfreeze(u, m, request.data, request.user)
        elif action == 'message':
            err = _message(u, request.data)
        elif action == 'reset_password':
            err = _reset_password(u, request.user)
        elif action == 'reset_2fa':
            err = reset_two_factor(u, request.user)
        elif action in ('deactivate', 'reactivate'):
            u.is_active = action == 'reactivate'
            u.save(update_fields=['is_active'])
            err = None
        else:
            return Response({'detail': f'Unknown action: {action}'}, status=400)
    if err:
        return Response({'detail': err}, status=400)
    return Response(_row(User.objects.select_related('membership__plan').get(id=u.id), timezone.localdate()))


def reset_two_factor(u, staff):
    """Turns someone's 2-step verification off. Staff roles set it up again at their next sign-in."""
    from apps.accounts.models import TwoFactor
    from apps.accounts.two_factor import is_required
    if not TwoFactor.objects.filter(user=u).exists():
        return f'{u.full_name or u.email} doesn’t have 2-step verification on.'
    from apps.accounts.two_factor import forget_devices
    TwoFactor.objects.filter(user=u).delete()
    forget_devices(u)
    logger.warning('2FA reset for %s by %s', u.email, staff.email)
    notify(u, 'system', bi('2-step verification was reset', 'تمت إعادة تعيين التحقق بخطوتين'),
           bi('The gym turned off 2-step verification on your account. ', 'أوقف النادي التحقق بخطوتين في حسابك. ')
           + (bi('You’ll set it up again the next time you sign in.', 'ستعيد إعداده عند تسجيل دخولك القادم.')
              if is_required(u)
              else bi('Turn it on again from your profile to keep your account safe.',
                      'فعّله مجددًا من ملفك الشخصي للحفاظ على أمان حسابك.'))
           + bi(' If you didn’t ask for this, contact the front desk.', ' إن لم تطلب ذلك فتواصل مع مكتب الاستقبال.'),
           icon='shield', priority='high',
           action_label='Security settings', action_path='/dashboard/profile?tab=security')
    return None


@api_view(['POST'])
@permission_classes([IsAdminUser])
def bulk_member_action(request):
    """POST /api/admin/members/bulk-action/ { action: message|freeze|export, member_ids, ... }"""
    ids = request.data.get('member_ids') or []
    if not isinstance(ids, list) or not ids:
        return Response({'detail': 'Select at least one member.'}, status=400)
    users = User.objects.filter(id__in=ids, role='member').select_related('membership__plan')
    action = request.data.get('action')
    if action == 'export':
        today = timezone.localdate()
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = 'attachment; filename="smart-gym-members.csv"'
        resp.write('﻿')
        w = csv.writer(resp)
        from apps.core.bilingual import header
        w.writerow(header(['Name', 'Email', 'Phone', 'Iqama', 'Plan', 'Expires', 'Status', 'Joined']))
        for u in users:
            r = _row(u, today)
            w.writerow([r['full_name'], r['email'], r['phone'], r['iqama_number'], r['plan'] or '',
                        r['expiry_date'] or '', r['membership_status'], u.created_at.date()])
        return resp
    done, errors = 0, []
    for u in users:
        if action == 'message':
            err = _message(u, request.data)
        elif action == 'freeze':
            m = Membership.objects.filter(member=u).first()
            err = _freeze(u, m, request.data, request.user) if m else 'no membership'
        else:
            return Response({'detail': f'Unknown action: {action}'}, status=400)
        if err:
            errors.append(f'{u.email}: {err}')
        else:
            done += 1
    return Response({'detail': f'{done} member(s) updated.', 'count': done, 'errors': errors})
