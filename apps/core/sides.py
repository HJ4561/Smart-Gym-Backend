"""
Two gyms, one backend: the male side and the female side. Every account and every record belongs to one
side, and nobody ever sees the other side's data.

How a request gets its side (SideMiddleware, then SideJWTAuthentication):
  1. the website it comes from (Origin / Referer): https://female-smart-gym.vercel.app → female,
     the male portal and public website → male; an `X-Gym-Side` header works for non-browser clients
     (the entrance script)
  2. once signed in, the account's own side — and an account can't be used from the other side's website

Every side-tagged model uses SideManager, so `Model.objects` only ever returns the current side's rows;
new rows get the current side automatically (`side_field()`). Outside a request (management commands,
webhooks with no Origin) there is no current side: queries see both sides and new rows are male unless
`using_side()` says otherwise.

Related lookups through a foreign key (obj.member) use Django's unfiltered base manager, which is fine:
a record only ever points at records of its own side.
"""
import contextvars
from contextlib import contextmanager
from urllib.parse import urlparse

from django.conf import settings
from django.db import models

MALE, FEMALE = 'male', 'female'
SIDES = (MALE, FEMALE)
SIDE_CHOICES = [(MALE, 'Male side'), (FEMALE, 'Female side')]

_current = contextvars.ContextVar('gym_side', default=None)


def female_hosts():
    return {urlparse(o).hostname for o in getattr(settings, 'FEMALE_FRONTEND_ORIGINS', [])}


def current_side():
    """'male' | 'female' | None (no request, or a request we can't place)."""
    return _current.get()


def side_default():
    """Default for side fields: the request's side, else male (everything before the split was male)."""
    return _current.get() or MALE


def set_side(side):
    return _current.set(side if side in SIDES else None)


def reset_side(token):
    _current.reset(token)


@contextmanager
def using_side(side):
    """Run code as one side (management commands, webhooks once the record's side is known)."""
    token = set_side(side)
    try:
        yield
    finally:
        reset_side(token)


@contextmanager
def all_sides():
    """Run code that must see both sides (webhooks before they know whose payment it is)."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def side_of_request(request):
    """The side the request's website belongs to, or None when it doesn't say (curl, webhooks)."""
    header = (request.META.get('HTTP_X_GYM_SIDE') or '').strip().lower()
    if header in SIDES:
        return header
    for key in ('HTTP_ORIGIN', 'HTTP_REFERER'):
        raw = request.META.get(key)
        if raw:
            host = urlparse(raw).hostname or ''
            return FEMALE if host in female_hosts() else MALE
    return None


def portal_url_for(side=None):
    """The member/staff portal of a side — for links in emails and notifications."""
    side = side or side_default()
    if side == FEMALE and getattr(settings, 'FEMALE_FRONTEND_ORIGINS', None):
        return settings.FEMALE_FRONTEND_ORIGINS[0].rstrip('/')
    return None


# ---------------------------------------------------------------- models
def side_field():
    return models.CharField(max_length=6, choices=SIDE_CHOICES, default=side_default, db_default=MALE,
                            db_index=True, editable=False)




class SideFilterMixin:
    """Manager mixin: only the current side's rows (all rows when there's no current side)."""

    def get_queryset(self):
        qs = super().get_queryset()
        side = _current.get()
        return qs.filter(side=side) if side else qs


class SideManager(SideFilterMixin, models.Manager):
    pass


OWNER_FIELDS = ('member', 'user', 'recipient', 'owner', 'author', 'submitted_by', 'actor', 'created_user', 'trainer',
                'nutritionist', 'course', 'gym_class', 'order', 'plan', 'cart', 'video', 'exercise', 'transaction')


def inherit_side(sender, instance, raw=False, **kwargs):
    """
    Safety net (pre_save): a new record saved with no current side — a management command, or code that
    runs outside a website request — takes the side of the person or thing it belongs to.
    """
    if raw or _current.get() or not instance._state.adding or not hasattr(instance, 'side'):
        return
    for name in OWNER_FIELDS:
        fid = getattr(instance, f'{name}_id', None)
        if fid:
            related = type(instance)._meta.get_field(name).related_model
            side = related._base_manager.filter(pk=fid).values_list('side', flat=True).first()                 if hasattr(related, 'side') else None
            if side:
                instance.side = side
                return


class SideMiddleware:
    """Sets the side for the whole request from the website it came from (refined after sign-in)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = set_side(side_of_request(request))
        request.gym_side = current_side()
        try:
            return self.get_response(request)
        finally:
            reset_side(token)
