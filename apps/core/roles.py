"""
Who works where. Every portal and back-office endpoint asks these helpers, so the rules live once.

  admin, manager     the admin console: everything (manager can't change admins or sign-in security)
  trainer            the coach portal
  nutritionist       the nutritionist portal
  receptionist       the front desk: only the modules an admin ticks on their staff profile
  shop_manager       the shop portal: orders directly, catalog changes go to the admin for approval
"""
from rest_framework.permissions import BasePermission

ADMIN_ROLES = ('admin', 'manager')
STAFF_ROLES = ('admin', 'manager', 'trainer', 'nutritionist', 'receptionist', 'shop_manager')

# Front-desk modules an admin can give a receptionist (key → English label, Arabic label).
DESK_MODULES = {
    'registrations': ('Website signups: approve or reject', 'تسجيلات الموقع: قبول أو رفض'),
    'gate': ('Entrance: scan passes, first-visit check, check-in and out', 'المدخل: مسح البطاقات والتحقق الأول وتسجيل الدخول والخروج'),
    'walk_in': ('Add a walk-in member at the desk', 'إضافة عضو حضوريًا من الاستقبال'),
    'members': ('Members list and member files (view)', 'قائمة الأعضاء وملفاتهم (عرض)'),
    'classes': ('Class timetable and rosters (view)', 'جدول الحصص وقوائم الحضور (عرض)'),
    'inbox': ('Website contact messages', 'رسائل التواصل من الموقع'),
}


def is_admin(user):
    return bool(user and user.is_authenticated and user.role in ADMIN_ROLES)


def modules_of(user):
    """The desk modules this person may use: all of them for admins, the ticked ones for a receptionist."""
    if is_admin(user) or (user and user.is_authenticated and user.is_staff):   # is_staff: Django-admin accounts
        return list(DESK_MODULES)
    if not (user and user.is_authenticated and user.role == 'receptionist'):
        return []
    profile = getattr(user, 'staff_profile', None)
    if profile is None or profile.status == 'suspended':
        return []
    return [m for m in (profile.modules or []) if m in DESK_MODULES]


def has_module(user, key):
    return key in modules_of(user)


def needs_approval(user):
    """Content from anyone but an admin or manager waits for one of them before members see it."""
    return not is_admin(user)


class IsAdminRole(BasePermission):
    def has_permission(self, request, view):
        return is_admin(request.user)


def DeskModule(*keys):
    """Permission class: an admin, or a receptionist given any of these modules."""
    class _DeskModule(BasePermission):
        message = 'You don’t have access to this part of the front desk. Ask an admin.'

        def has_permission(self, request, view):
            return any(has_module(request.user, k) for k in keys)
    _DeskModule.__name__ = f'DeskModule_{"_".join(keys)}'
    return _DeskModule


def DeskRead(key, also_write=()):
    """Permission class: admins do anything; a receptionist with `key` may read (GET), and also use the
    write methods listed in `also_write` (e.g. ('PATCH',) to mark an inbox message read)."""
    class _DeskRead(BasePermission):
        message = 'You don’t have access to this part of the front desk. Ask an admin.'

        def has_permission(self, request, view):
            if is_admin(request.user):
                return True
            ok_method = request.method in ('GET', 'HEAD', 'OPTIONS') or request.method in also_write
            return ok_method and has_module(request.user, key)
    _DeskRead.__name__ = f'DeskRead_{key}'
    return _DeskRead


class IsShopStaff(BasePermission):
    """The shop portal: admins, managers and the shop manager."""
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and (u.role in ADMIN_ROLES or u.role == 'shop_manager'))


class IsNutritionist(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == 'nutritionist')
