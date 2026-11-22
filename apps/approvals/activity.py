"""
Staff activity log: one row for every change a staff member makes (successful POST, PUT, PATCH or
DELETE), so the admin can check who did what and when. Members' own actions aren't logged here.
Request bodies are never stored (they can hold passwords or personal data) — only what and where.
"""
import logging
import re

from apps.core.roles import STAFF_ROLES

logger = logging.getLogger(__name__)

WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')
SKIP = re.compile(r'^/api/(auth/|notifications/|admin/activity/|public/|payments/(checkout|intents)/)')

AREAS = [  # path prefix → area shown in the log (first match wins)
    ('/api/admin/registrations-api/', 'front desk'), ('/api/admin/approvals/', 'approvals'),
    ('/api/admin/shop/', 'shop'), ('/api/shop-manager/', 'shop'), ('/api/admin/zatca/', 'e-invoicing'),
    ('/api/admin/site-content/', 'website'), ('/api/admin/finance/', 'finance'), ('/api/admin/payments/', 'finance'),
    ('/api/admin/members/', 'members'), ('/api/admin/staff/', 'team'), ('/api/admin/trainers/', 'coaches'),
    ('/api/admin/trainer-', 'coaches'), ('/api/admin/courses/', 'courses'), ('/api/admin/machines/', 'machines'),
    ('/api/admin/machine-videos/', 'machines'), ('/api/admin/nutrition/', 'nutrition'),
    ('/api/admin/settings/', 'settings'), ('/api/admin/facility-pricing/', 'facilities'),
    ('/api/classes/', 'classes'), ('/api/coach/', 'coach portal'), ('/api/nutritionist/', 'nutritionist portal'),
    ('/api/desk/', 'front desk'), ('/api/reports/', 'reports'), ('/api/admin/', 'admin'),
]
VERBS = {'POST': 'Added', 'PUT': 'Saved', 'PATCH': 'Edited', 'DELETE': 'Deleted'}
ACTION_WORDS = {'approve', 'reject', 'refund', 'status', 'cancel', 'submit', 'check-in', 'check-out', 'face-verify',
                'bulk-approve', 'reset', 'regenerate-qr', 'send-sign-in', 'reset-2fa', 'portal-access', 'reorder',
                'email-receipt', 'retry', 'send', 'debit', 'activate', 'action', 'bulk-action', 'withdraw',
                'reschedule', 'photo', 'cover', 'image', 'logo', 'walk-in', 'generate'}


def describe(method, path):
    """("PATCH", "/api/admin/shop/products/3/") → ("Edited product", "shop")."""
    area = next((a for prefix, a in AREAS if path.startswith(prefix)), 'other')
    words = [w for w in path.strip('/').split('/')[1:] if w and not w.isdigit() and w not in ('admin', 'api')]
    last = words[-1] if words else ''
    if last in ACTION_WORDS:
        noun = words[-2] if len(words) > 1 else ''
        action = f'{last.replace("-", " ").capitalize()} {_single(noun)}'
    else:
        action = f'{VERBS.get(method, method)} {_single(last)}'
    return action.strip()[:120], area


def _single(word):
    word = word.replace('-', ' ').replace('_', ' ')
    if word.endswith('ies'):
        return word[:-3] + 'y'
    if word.endswith('s') and not word.endswith('ss'):
        return word[:-1]
    return word


class StaffActivityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if request.method in WRITE_METHODS and 200 <= response.status_code < 400 \
                    and request.path.startswith('/api/') and not SKIP.match(request.path):
                user = getattr(request, 'user', None)     # DRF sets this after JWT authentication
                if user is not None and user.is_authenticated and user.role in STAFF_ROLES:
                    from .models import StaffActivity
                    action, area = describe(request.method, request.path)
                    StaffActivity.objects.create(actor=user, role=user.role, method=request.method,
                                                 path=request.path[:250], status_code=response.status_code,
                                                 action=action, area=area)
        except Exception:   # noqa: BLE001 — logging must never break the request
            logger.exception('Could not record staff activity for %s %s', request.method, request.path)
        return response
