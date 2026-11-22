"""
What each kind of approvable content needs, and the queue's actions.

A kind registers:
  label / label_ar   shown in the admin's queue ("Shop product")
  model              the Django model
  describe(obj)      one line: "Whey Isolate 1kg — SAR 25.00"
  snapshot(obj)      the fields the admin compares, as a dict
  apply(obj, data)   validates `data` and copies it onto obj in memory → {field: error}
  publish(obj)       makes a new item visible (sets its approval flag, saves)
  hide(obj)          marks a rejected new item (it stays visible to the person who sent it)
  remove(obj)        carries out an approved delete request
  owner_path / admin_path(obj)   where the submitter and the admin open it
  editable           fields the admin may change before approving (shown as inputs in the queue)
  save(obj)          optional: how an approved change is stored (default obj.save())
"""
import logging

from django.db import transaction
from django.utils import timezone

from .models import ChangeRequest

logger = logging.getLogger(__name__)

KINDS = {}


class Kind:
    def __init__(self, key, *, label, label_ar, model, describe, snapshot, apply=None, publish=None, hide=None,
                 remove=None, owner_path='', admin_path='', editable=(), save=None):
        self.key, self.label, self.label_ar, self.model = key, label, label_ar, model
        self.save = save or (lambda obj: obj.save())
        self.describe, self.snapshot = describe, snapshot
        self.apply, self.publish, self.hide, self.remove = apply, publish, hide, remove
        self.owner_path, self.admin_path = owner_path, admin_path
        self.editable = tuple(editable)

    def path(self, which, obj):
        p = self.owner_path if which == 'owner' else self.admin_path
        return p(obj) if callable(p) else p


def register(key, **kw):
    KINDS[key] = Kind(key, **kw)
    return KINDS[key]


def kind_of(cr):
    return KINDS.get(cr.target_type)


def target(cr):
    k = kind_of(cr)
    return k.model.objects.filter(pk=cr.object_id).first() if k else None


def _clean(value):
    """JSON-safe copy of a snapshot value."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    return str(value)


# ---------------------------------------------------------------- sending
def submit_create(user, key, obj, summary=''):
    """A new item saved hidden; it waits in the queue until an admin publishes it."""
    k = KINDS[key]
    cr = ChangeRequest.objects.create(target_type=key, object_id=obj.pk, kind='create', submitted_by=user,
                                      summary=(summary or k.describe(obj))[:200], before=_clean(k.snapshot(obj)))
    _tell_admins(cr, k, obj, user)
    return cr


def submit_update(user, key, obj, data, summary=''):
    """
    Proposed new values for a live item. `data` is checked against a copy first, so mistakes come
    back now ({field: error}), not when the admin approves. One waiting change per item and person:
    sending again merges into it. Returns (ChangeRequest or None, errors).
    """
    k = KINDS[key]
    probe = k.model.objects.get(pk=obj.pk)
    errors = k.apply(probe, data) or {}
    if errors:
        return None, errors
    before, after = k.snapshot(obj), k.snapshot(probe)
    changed = {f: _clean(v) for f, v in after.items() if before.get(f) != v}
    if not changed:
        return None, {}
    with transaction.atomic():
        cr = (ChangeRequest.objects.select_for_update()
              .filter(target_type=key, object_id=obj.pk, kind='update', status='pending', submitted_by=user).first())
        # Only what actually changes (forms send every field); keys the snapshot doesn't show are kept.
        keep = {f: data[f] for f in data if f in changed or f not in after}
        payload = {**(cr.payload if cr else {}), **_clean(keep)}
        if cr:
            cr.payload = payload
            cr.summary = (summary or f'Changes to {k.describe(obj)}')[:200]
            cr.save(update_fields=['payload', 'summary', 'updated_at'])
        else:
            cr = ChangeRequest.objects.create(
                target_type=key, object_id=obj.pk, kind='update', submitted_by=user, payload=payload,
                before=_clean(before), summary=(summary or f'Changes to {k.describe(obj)}')[:200])
            _tell_admins(cr, k, obj, user)
    return cr, {}


def submit_delete(user, key, obj, summary=''):
    k = KINDS[key]
    cr, _ = ChangeRequest.objects.get_or_create(
        target_type=key, object_id=obj.pk, kind='delete', status='pending',
        defaults={'submitted_by': user, 'summary': (summary or f'Remove {k.describe(obj)}')[:200],
                  'before': _clean(k.snapshot(obj))})
    _tell_admins(cr, k, obj, user)
    return cr


def pending_for(key, obj_id, user=None):
    qs = ChangeRequest.objects.filter(target_type=key, object_id=obj_id, status='pending')
    return qs.filter(submitted_by=user) if user else qs


# ---------------------------------------------------------------- deciding
class ReviewError(Exception):
    def __init__(self, errors):
        super().__init__(str(errors))
        self.errors = errors


def approve(cr, admin, payload=None, note=''):
    """
    Publishes a new item, applies a change (with the admin's edits in `payload`, if any) or removes
    an item. Raises ReviewError({field: error}) when the values don't pass validation.
    """
    k = kind_of(cr)
    if cr.status != 'pending' or k is None:
        raise ReviewError({'detail': 'This request was already handled.'})
    obj = target(cr)
    if obj is None:
        cr.status, cr.review_note = 'withdrawn', 'The item no longer exists.'
        cr.save(update_fields=['status', 'review_note', 'updated_at'])
        raise ReviewError({'detail': 'The item no longer exists.'})
    with transaction.atomic():
        if cr.kind == 'delete':
            k.remove(obj)
        else:
            data = payload if payload is not None else (cr.payload if cr.kind == 'update' else {})
            if data:
                errors = k.apply(obj, data) or {}
                if errors:
                    raise ReviewError(errors)
                if cr.kind == 'update':
                    cr.payload = _clean(data)
            if cr.kind == 'create':
                k.publish(obj)       # saves
            else:
                k.save(obj)
        cr.status, cr.reviewed_by, cr.reviewed_at, cr.review_note = 'approved', admin, timezone.now(), note[:2000]
        cr.save()
    _tell_submitter(cr, k, obj, approved=True)
    return cr


def reject(cr, admin, note=''):
    k = kind_of(cr)
    if cr.status != 'pending' or k is None:
        raise ReviewError({'detail': 'This request was already handled.'})
    obj = target(cr)
    with transaction.atomic():
        if cr.kind == 'create' and obj is not None and k.hide:
            k.hide(obj)
        cr.status, cr.reviewed_by, cr.reviewed_at, cr.review_note = 'rejected', admin, timezone.now(), note[:2000]
        cr.save()
    _tell_submitter(cr, k, obj, approved=False)
    return cr


# ---------------------------------------------------------------- telling people
def _tell_admins(cr, k, obj, user):
    from apps.core.i18n import bi
    from apps.notifications.service import notify_staff
    if user is not None and user.role in ('admin', 'manager'):
        return
    who = (user.full_name or user.email) if user else 'Someone'
    what = {'create': ('New', 'جديد'), 'update': ('Change to', 'تعديل على'), 'delete': ('Remove', 'حذف')}[cr.kind]
    notify_staff('system', bi(f'Waiting for approval: {k.label}', f'بانتظار الموافقة: {k.label_ar}'),
                 bi(f'{who} sent: {what[0]} {cr.summary}.', f'أرسل {who}: {what[1]} {cr.summary}.'),
                 icon='fact_check', priority='normal', action_label='Review', action_path=f'/admin/approvals?open={cr.id}')


def _tell_submitter(cr, k, obj, approved):
    from apps.core.i18n import bi
    from apps.notifications.service import notify
    user = cr.submitted_by
    if user is None or not user.is_active or user.role in ('admin', 'manager'):
        return
    note = f' Note: {cr.review_note}' if cr.review_note else ''
    note_ar = f' ملاحظة: {cr.review_note}' if cr.review_note else ''
    if approved:
        title = bi(f'Approved: {cr.summary}', f'تمت الموافقة: {cr.summary}')
        body = bi(f'The gym approved your {k.label.lower()}. It’s live now.{note}',
                  f'وافق النادي على {k.label_ar}. أصبح ظاهرًا الآن.{note_ar}')
    else:
        title = bi(f'Not approved: {cr.summary}', f'لم تتم الموافقة: {cr.summary}')
        body = bi(f'The gym didn’t approve your {k.label.lower()}.{note}',
                  f'لم يوافق النادي على {k.label_ar}.{note_ar}')
    path = k.path('owner', obj) if obj is not None else ''
    notify(user, 'system', title, body, icon='fact_check', priority='high' if not approved else 'normal',
           action_label='Open' if path else '', action_path=path)
