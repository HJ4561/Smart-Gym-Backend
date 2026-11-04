"""
Admin → Settings: what this deployment is actually configured to do. Mostly read-only — these
values come from the server's environment variables. Never returns secrets, only whether they're
set. The one switch kept in the database: 2-step verification for the whole gym (security/).
"""
from django.conf import settings
from django.db import connection
from django.db.models import Sum
import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.core.i18n import bi

from .permissions import IsAdminUser

logger = logging.getLogger(__name__)

ACCESS = [   # mirrors the checks in the code (IsAdminUser, staff_ops._guard, members_ops role rules)
    {'role': 'Admin', 'portal': True, 'can': [
        'Everything in this admin portal',
        'Add, edit and suspend admins and managers',
        'Change member and staff roles',
    ]},
    {'role': 'Manager', 'portal': True, 'can': [
        'Everything in this admin portal',
        'Add and edit trainers, nutritionists and receptionists',
    ], 'cannot': ['Change admins or managers', 'Change roles']},
    {'role': 'Coach (trainer)', 'portal': False, 'can': [
        'The coach portal (/coach): own hours, sessions, courses, students, classes, machine videos, earnings',
    ], 'cannot': ['Sign in to this admin portal', 'Publish courses or change session prices']},
    {'role': 'Nutritionist · Receptionist', 'portal': False, 'can': [
        'Listed on the staff roster for scheduling',
    ], 'cannot': ['Sign in to this admin portal']},
    {'role': 'Member', 'portal': False, 'can': ['The member portal: pass, payments, classes, courses, shop']},
]


def _moyasar_mode():
    key = settings.MOYASAR_SECRET_KEY or ''
    if key.startswith('sk_live_'):
        return 'live'
    if key.startswith('sk_test_'):
        return 'test'
    return 'not set' if not key else 'unknown key'


def _capacity():
    if settings.GYM_CAPACITY:
        return {'value': settings.GYM_CAPACITY, 'source': 'GYM_CAPACITY'}
    from apps.members.models import FacilityAddon
    total = FacilityAddon.objects.filter(item__is_active=True).aggregate(t=Sum('capacity'))['t'] or 0
    return {'value': total, 'source': 'sum of facility capacities (Facilities page)'}


@api_view(['GET'])
@permission_classes([IsAdminUser])
def system_status(request):
    """GET /api/admin/settings/"""
    email_backend = settings.EMAIL_BACKEND.rsplit('.', 2)[-2]
    smtp = email_backend == 'smtp'
    return Response({
        'payments': {
            'provider': 'Moyasar',
            'mode': _moyasar_mode(),
            'publishable_key_set': bool(settings.MOYASAR_PUBLISHABLE_KEY),
            'webhook_secret_set': bool(settings.MOYASAR_WEBHOOK_SECRET),
            'currency': 'SAR',
            'vat_rate_percent': round(float(settings.SHOP_VAT_RATE) * 100, 2),
        },
        'email': {
            'sending': smtp,
            'method': f'SMTP ({settings.EMAIL_HOST})' if smtp else 'Not sending — emails are only written to the server log',
            'from': settings.DEFAULT_FROM_EMAIL,
            'contact_form_notifies': settings.CONTACT_NOTIFY_EMAIL or None,
        },
        'storage': {
            'uploads': (f'Cloudinary (folder “{settings.CLOUDINARY_FOLDER}”)' if settings.USE_CLOUDINARY
                        else 'Server disk — uploads are lost on redeploy'),
            'durable': settings.USE_CLOUDINARY,
        },
        'gym': {
            'time_zone': settings.TIME_ZONE,
            'capacity': _capacity(),
            'portal_url': [u.strip() for u in settings.FRONTEND_URL.split(',') if u.strip()],
        },
        'server': {
            'database': {'postgresql': 'PostgreSQL', 'sqlite': 'SQLite'}.get(connection.vendor, connection.vendor),
            'debug': settings.DEBUG,
        },
        'access': ACCESS,
        'security': _security(),
        'me': {'email': request.user.email, 'name': request.user.full_name, 'role': request.user.role},
    })


def _security():
    from apps.accounts.two_factor import TRUST_DAYS, email_available, policy
    return {**policy(), 'trust_days': TRUST_DAYS, 'email_codes': email_available()}


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def security_settings(request):
    """
    PATCH /api/admin/settings/security/ {two_factor_enabled?, two_factor_required_for_staff?, password}
    Admins only, with their password. Off = everyone signs in with email + password only (people
    keep their 2-step setup and it works again when switched back on). Every admin is told.
    """
    from apps.notifications.service import notify_staff
    from .models import SystemSettings
    if request.user.role != 'admin':
        return Response({'detail': 'Only an admin can change sign-in security.'}, status=403)
    if not request.user.check_password(str(request.data.get('password') or '')):
        return Response({'password': 'Your password isn’t right.'}, status=400)
    s = SystemSettings.get_solo()
    sec = dict(s.security or {})
    changed, changed_ar = [], []
    for key, label, label_ar in (('two_factor_enabled', '2-step verification', 'التحقق بخطوتين'),
                                 ('two_factor_required_for_staff', '2-step verification required for staff and coaches',
                                  'إلزام الموظفين والمدربين بالتحقق بخطوتين')):
        if key in request.data:
            value = bool(request.data[key])
            before = sec.get(key, True) is not False
            if before != value:
                changed.append(f'{label}: {"on" if value else "off"}')
                changed_ar.append(f'{label_ar}: {"مفعّل" if value else "متوقف"}')
            sec[key] = value
    s.security = sec
    s.updated_by = request.user
    s.save(update_fields=['security', 'updated_by', 'updated_at'])
    if changed:
        logger.warning('Security settings changed by %s: %s', request.user.email, '; '.join(changed))
        who = request.user.full_name or request.user.email
        notify_staff('system', bi('Sign-in security changed', 'تم تغيير أمان تسجيل الدخول'),
                     bi(f'{who} set ' + '; '.join(changed) + '.',
                        f'غيّر {who}: ' + '؛ '.join(changed_ar) + '.'), icon='shield', priority='high',
                     action_label='Open settings', action_path='/admin/settings')
    return Response(_security())
