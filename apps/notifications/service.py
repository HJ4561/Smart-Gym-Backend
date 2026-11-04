"""
Create member notifications for real events (payments, bookings, check-ins, ...).
The member portal's bell and Notifications page read these.

Titles and messages are given in both languages — bi('Order paid', 'تم دفع الطلب') — and both are
stored; the API returns the reader's. A plain English string still works: its Arabic comes from the
message catalogue (apps.core.messages_ar), or it stays English.
"""
import logging

from apps.core.i18n import both

from .models import Notification

logger = logging.getLogger(__name__)


def notify(member, category, title, message, *, icon='notifications', priority='normal',
           action_label='', action_path=''):
    """Never raises: a failed notification must not break the action that triggered it."""
    try:
        title_en, title_ar = both(title)
        message_en, message_ar = both(message)
        return Notification.objects.create(
            recipient=member, category=category, priority=priority, icon=icon,
            title=title_en[:200], message=message_en, title_ar=title_ar[:200], message_ar=message_ar,
            action_label=action_label[:80], action_path=action_path[:200],
        )
    except Exception:   # noqa: BLE001
        logger.exception('Could not create notification %r for user %s', title, getattr(member, 'id', None))
        return None


def notify_staff(category, title, message, *, icon='notifications', priority='normal', action_label='',
                 action_path=''):
    """The same notification to every active admin and manager (the admin portal's bell)."""
    from apps.accounts.models import User
    for staff in User.objects.filter(role__in=('admin', 'manager'), is_active=True):
        notify(staff, category, title, message, icon=icon, priority=priority,
               action_label=action_label, action_path=action_path)


def notify_coach(trainer_profile, category, title, message, *, icon='notifications', priority='normal',
                 action_label='', action_path=''):
    """A notification for a coach — shown in the coach portal once they can sign in."""
    user = getattr(trainer_profile, 'user', None)
    if user is None or not user.is_active:
        return None
    return notify(user, category, title, message, icon=icon, priority=priority,
                  action_label=action_label, action_path=action_path)
