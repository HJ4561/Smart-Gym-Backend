"""
Weekly group classes. A booking is a standing weekly spot in one class (enrolled), or a place
in its waitlist; when someone leaves, the earliest waitlisted member moves up automatically.
Paid classes are locked until paid — the whole class or single sessions (apps.classes.passes,
paid through the payments gateway with purpose "class_pass").

Members:  GET  /api/classes/                 this week's timetable with my booking state
          POST /api/classes/<id>/book/       spot, or waitlist when full
          POST /api/classes/<id>/cancel/     frees the spot (promotes the waitlist)
Admin:    GET/POST   /api/classes/admin/                 all classes / add one
          PATCH/DELETE /api/classes/admin/<id>/          edit, pause (is_active) or remove
          GET  /api/classes/admin/<id>/roster/           who's booked and waitlisted
          POST /api/classes/admin/<id>/roster/<enrollment_id>/remove/
          GET  /api/classes/schedule/ · /api/classes/stats/
"""
import re
from datetime import time

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.admin_api.permissions import IsAdminUser
from apps.catalog.models import TrainerProfile
from apps.notifications.service import notify, notify_coach, notify_staff

from . import passes
from .models import ClassEnrollment, ClassPass, GymClass

DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
CATEGORIES = dict(GymClass.CATEGORY_CHOICES)
LEVELS = dict(GymClass.LEVEL_CHOICES)
BOOKED = ['enrolled', 'attended']


def _annotated(qs):
    return qs.select_related('trainer__user').annotate(
        booked=Count('enrollments', filter=Q(enrollments__status__in=BOOKED), distinct=True),
        waiting=Count('enrollments', filter=Q(enrollments__status='waitlist'), distinct=True),
    )


def _row(c, mine=None):
    booked = getattr(c, 'booked', None)
    if booked is None:
        booked = c.enrollments.filter(status__in=BOOKED).count()
    waiting = getattr(c, 'waiting', None)
    if waiting is None:
        waiting = c.enrollments.filter(status='waitlist').count()
    trainer = c.trainer.user.full_name if c.trainer_id and c.trainer.user.full_name else (
        c.trainer.user.email if c.trainer_id else '')
    end_min = c.start_time.hour * 60 + c.start_time.minute + c.duration_minutes
    offer = passes.full_offer(c)
    row = {
        'id': c.id, 'name': c.name, 'category': c.category, 'category_label': CATEGORIES.get(c.category, c.category),
        'level': c.level, 'level_label': LEVELS.get(c.level, c.level), 'description': c.description,
        'trainer_id': c.trainer_id, 'trainer': trainer,
        'day_of_week': c.day_of_week, 'day': DAYS[c.day_of_week] if 0 <= c.day_of_week < 7 else '',
        'start_time': c.start_time.strftime('%H:%M'),
        'end_time': f'{(end_min // 60) % 24:02d}:{end_min % 60:02d}',
        'duration_minutes': c.duration_minutes, 'capacity': c.capacity, 'location': c.location,
        'is_active': c.is_active, 'booked': booked, 'waitlisted': waiting,
        'spots_left': max(c.capacity - booked, 0), 'status': 'full' if booked >= c.capacity else 'open',
        'is_paid': passes.is_paid(c), 'price_per_session': str(c.price_per_session or 0),
        'full_price': str(c.full_price) if c.full_price is not None else None,
        'starts_on': c.starts_on, 'ends_on': c.ends_on,
        'full_offer': ({**offer, 'price': str(offer['price'])} if offer else None),
    }
    if mine is not None:
        row['my_status'] = mine.get(c.id)        # enrolled | waitlist | None
        if row['my_status'] == 'waitlist':
            row['my_waitlist_position'] = (ClassEnrollment.objects.filter(
                gym_class=c, status='waitlist', enrolled_at__lt=mine.get(('at', c.id))).count() + 1)
    return row


def _promote_waitlist(c):
    """Moves the earliest waitlisted members into free spots and tells them."""
    with transaction.atomic():
        c = GymClass.objects.select_for_update().get(id=c.id)
        free = c.capacity - c.enrollments.filter(status__in=BOOKED).count()
        for e in c.enrollments.filter(status='waitlist').select_related('member').order_by('enrolled_at')[:max(free, 0)]:
            e.status = 'enrolled'
            e.save(update_fields=['status'])
            notify(e.member, 'attendance', 'You’re in!',
                   f'A spot opened in {c.name} ({DAYS[c.day_of_week]}s at {c.start_time:%H:%M}). It’s yours.',
                   icon='event_available', action_label='My classes', action_path='/dashboard/classes')


# ============================================================
# MEMBERS
# ============================================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def member_classes(request):
    """
    GET /api/classes/ — active classes, Monday → Sunday, with the caller's booking state. Paid
    classes also carry what the member paid for (`my_access`), whether they're `locked`, and the
    upcoming single-session dates with free spots.
    """
    mine = {}
    for e in ClassEnrollment.objects.filter(member=request.user).exclude(status='cancelled'):
        if e.status in ('enrolled', 'attended', 'waitlist'):
            mine[e.gym_class_id] = 'waitlist' if e.status == 'waitlist' else 'enrolled'
            mine[('at', e.gym_class_id)] = e.enrolled_at
    today = timezone.localdate()
    classes = (_annotated(GymClass.objects.filter(is_active=True).exclude(ends_on__lt=today))
               .order_by('day_of_week', 'start_time'))
    rows = []
    for c in classes:
        row = _row(c, mine)
        if row['is_paid']:
            access = passes.member_access(request.user, c, today)
            row['my_access'] = access
            row['locked'] = not (access['full'] or access['sessions'] or row['my_status'] == 'enrolled')
            row['session_dates'] = [{'date': d, 'spots_left': max(c.capacity - passes.booked_on(c, d), 0)}
                                    for d in passes.bookable_dates(c, today)] if (c.price_per_session or 0) > 0 else []
        else:
            row['locked'] = False
        rows.append(row)
    return Response({'days': DAYS, 'classes': rows})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def book_class(request, class_id):
    """POST /api/classes/<id>/book/ — needs a membership that allows gym entry."""
    ms = getattr(request.user, 'membership', None)
    if ms is None or not ms.allows_entry:
        return Response({'detail': 'You need an active membership to book classes.'}, status=403)
    with transaction.atomic():
        c = get_object_or_404(GymClass.objects.select_for_update(), id=class_id, is_active=True)
        if passes.is_paid(c):
            offer = passes.full_offer(c)
            return Response({'detail': f'{c.name} is a paid class — pay for the whole class or a single session to join.',
                             'payment_required': True, 'price_per_session': str(c.price_per_session or 0),
                             'full_offer': ({**offer, 'price': str(offer['price'])} if offer else None)}, status=402)
        if passes.has_ended(c):
            return Response({'detail': 'This class has finished.'}, status=400)
        e = ClassEnrollment.objects.filter(gym_class=c, member=request.user).first()
        if e and e.status in ('enrolled', 'attended', 'waitlist'):
            return Response({'detail': 'You’re already booked into this class.'}, status=400)
        booked = c.enrollments.filter(status__in=BOOKED).count()
        status = 'enrolled' if booked < c.capacity else 'waitlist'
        if e:
            e.status = status
            e.save(update_fields=['status'])
            ClassEnrollment.objects.filter(id=e.id).update(enrolled_at=timezone.now())   # back of the queue
        else:
            ClassEnrollment.objects.create(gym_class=c, member=request.user, status=status)
    msg = (f'Booked: {c.name}, every {DAYS[c.day_of_week]} at {c.start_time:%H:%M}.' if status == 'enrolled'
           else f'{c.name} is full — you’re on the waitlist and we’ll tell you when a spot opens.')
    return Response({'detail': msg, 'status': status, 'class': _row(c)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_class(request, class_id):
    """
    POST /api/classes/<id>/cancel/            — free class: frees the standing spot
    POST /api/classes/<id>/cancel/ {date}     — paid single session: refunded to locker credit up to
                                                24 h before it starts
    A paid whole-class spot is refunded by the front desk (Finance → payments).
    """
    c = get_object_or_404(GymClass, id=class_id)
    if passes.is_paid(c):
        return _cancel_paid(request, c)
    e = ClassEnrollment.objects.filter(gym_class=c, member=request.user, status__in=BOOKED + ['waitlist']).first()
    if e is None:
        return Response({'detail': 'You aren’t booked into this class.'}, status=400)
    was_booked = e.status in BOOKED
    e.status = 'cancelled'
    e.save(update_fields=['status'])
    if was_booked:
        _promote_waitlist(c)
    who = request.user.full_name or request.user.email
    _tell_team(c, 'Class booking cancelled',
               f'{who} cancelled their {"spot" if was_booked else "waitlist place"} in {c.name} '
               f'({DAYS[c.day_of_week]}s at {c.start_time:%H:%M}).')
    return Response({'detail': f'Cancelled — you’re no longer booked into {c.name}.'})


REFUND_HOURS = 24


def _tell_team(c, title, message, *, staff=True):
    """A cancellation the class's coach and the admin portal should know about, not only the member."""
    if c.trainer_id:
        notify_coach(c.trainer, 'attendance', title, message, icon='event_busy',
                     action_label='My classes', action_path='/coach/classes')
    if staff:
        notify_staff('attendance', title, message, icon='event_busy',
                     action_label='Open classes', action_path='/admin/classes')


def _cancel_paid(request, c):
    from datetime import datetime, timedelta
    from django.db.models import F
    from apps.payments.gateway import paid_transaction, record_transaction
    from apps.payments.models import LockerCredit
    day = passes.parse_day(request.data.get('date'))
    if day is None:
        return Response({'detail': 'Whole-class bookings are refunded by the front desk — ask them to help.'}, status=400)
    with transaction.atomic():
        p = ClassPass.objects.select_for_update().filter(gym_class=c, member=request.user, kind='session',
                                                         session_date=day, is_cancelled=False).first()
        if p is None:
            return Response({'detail': 'You don’t have a paid session on that date.'}, status=400)
        start = timezone.make_aware(datetime.combine(day, c.start_time), timezone.get_current_timezone())
        if start - timezone.now() < timedelta(hours=REFUND_HOURS):
            return Response({'detail': f'Sessions can be cancelled up to {REFUND_HOURS} hours before they start.'}, status=400)
        p.is_cancelled = True
        p.save(update_fields=['is_cancelled'])
        credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=request.user)
        LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') + p.amount)
        record_transaction(request.user, f'Refund — {c.name} on {day:%d %b}', 'refund', -p.amount,
                           'Locker credit', f'class-pass:{p.id}',
                           refund_of=paid_transaction(member=request.user, purpose='class_pass', gym_class=c,
                                                      pass_kind='session', session_date=day))
    notify(request.user, 'attendance', 'Session cancelled', f'SAR {p.amount} for {c.name} on {day:%a %d %b} is back '
           'in your locker credit.', icon='event_busy', action_label='View payments', action_path='/dashboard/payments')
    _tell_team(c, 'Class session cancelled',
               f'{request.user.full_name or request.user.email} cancelled their paid session of {c.name} on '
               f'{day:%a %d %b} (SAR {p.amount} refunded to locker credit).')
    return Response({'detail': f'Cancelled — SAR {p.amount} is back in your locker credit.'})


# ============================================================
# ADMIN
# ============================================================
def _parse_time(v):
    try:
        h, m = str(v).split(':')[:2]
        return time(int(h), int(m))
    except (TypeError, ValueError):
        return None


CLASS_NAME_MIN, CLASS_NAME_MAX = 3, 60
# Rooms: a word and a number/letter — "Studio 2", "Room 101", "Hall B", "Pool 1A".
ROOM_RE = re.compile(r'[A-Za-z]{3,15} [A-Za-z0-9]{1,4}')
ROOM_MESSAGE = 'Room must be a word and a number or letter, e.g. “Studio 2”, “Room 101” or “Hall B” (max 20 characters).'


def _minutes(t):
    return t.hour * 60 + t.minute


def _conflicts(c, errors):
    """Same weekday, overlapping times: one coach can't teach two classes, one room can't host two."""
    if not c.is_active or c.start_time is None or c.day_of_week is None:
        return
    start = _minutes(c.start_time)
    end = start + (c.duration_minutes or 0)
    others = GymClass.objects.filter(is_active=True, day_of_week=c.day_of_week).select_related('trainer__user')
    if c.id:
        others = others.exclude(id=c.id)
    for o in others:
        o_start = _minutes(o.start_time)
        o_end = o_start + (o.duration_minutes or 0)
        if not (start < o_end and o_start < end):
            continue
        when = f'{DAYS[o.day_of_week]} {o.start_time:%H:%M}–{(o_end // 60) % 24:02d}:{o_end % 60:02d}'
        if c.trainer_id and o.trainer_id == c.trainer_id:
            errors.setdefault('trainer_id', f'{o.trainer.user.full_name or "This coach"} already teaches “{o.name}” '
                                            f'on {when}. A coach can’t be in two classes at once.')
        if c.location and o.location and c.location.casefold() == o.location.casefold():
            errors.setdefault('location', f'{o.location} is already booked for “{o.name}” on {when}. Pick another room or time.')
        if c.name.casefold() == o.name.casefold() and o_start == start:
            errors.setdefault('name', f'“{o.name}” already runs on {when}.')


def _apply(c, data, errors):
    if 'name' in data:
        c.name = ' '.join(str(data.get('name') or '').split())
        if not c.name:
            errors['name'] = 'Class name is required.'
        elif not CLASS_NAME_MIN <= len(c.name) <= CLASS_NAME_MAX:
            errors['name'] = f'Class name must be {CLASS_NAME_MIN}–{CLASS_NAME_MAX} characters.'
    if 'category' in data:
        if data.get('category') not in CATEGORIES:
            errors['category'] = 'Choose a category.'
        c.category = data.get('category')
    if 'level' in data:
        if data.get('level') not in LEVELS:
            errors['level'] = 'Choose a level.'
        c.level = data.get('level')
    if 'description' in data:
        c.description = str(data.get('description') or '')[:2000]
    if 'location' in data:
        c.location = ' '.join(str(data.get('location') or '').split())
        if c.location and not ROOM_RE.fullmatch(c.location):
            errors['location'] = ROOM_MESSAGE
        elif c.location:
            c.location = c.location[0].upper() + c.location[1:]
    if 'trainer_id' in data:
        tid = data.get('trainer_id')
        if tid in (None, ''):
            c.trainer = None
        else:
            t = TrainerProfile.objects.filter(id=tid).first()
            if t is None:
                errors['trainer_id'] = 'Choose a trainer.'
            c.trainer = t
    if 'day_of_week' in data:
        try:
            d = int(data.get('day_of_week'))
            if not 0 <= d <= 6:
                raise ValueError
            c.day_of_week = d
        except (TypeError, ValueError):
            errors['day_of_week'] = 'Choose a day.'
    if 'start_time' in data:
        t = _parse_time(data.get('start_time'))
        if t is None:
            errors['start_time'] = 'Use HH:MM.'
        c.start_time = t
    for f, lo, hi in (('duration_minutes', 10, 300), ('capacity', 1, 500)):
        if f in data:
            try:
                n = int(data.get(f))
                if not lo <= n <= hi:
                    raise ValueError
                setattr(c, f, n)
            except (TypeError, ValueError):
                errors[f] = f'Enter a number from {lo} to {hi}.'
    if 'is_active' in data:
        c.is_active = bool(data.get('is_active'))
    _apply_paid(c, data, errors)


def _apply_paid(c, data, errors):
    """Price per session (0 = free), optional whole-class price, and the dates the class runs."""
    from decimal import Decimal, InvalidOperation
    for f in ('price_per_session', 'full_price'):
        if f in data:
            raw = data.get(f)
            if raw in (None, '') and f == 'full_price':
                c.full_price = None
                continue
            try:
                v = Decimal(str(raw if raw not in (None, '') else 0)).quantize(Decimal('0.01'))
                if not Decimal('0') <= v <= Decimal('10000'):
                    raise InvalidOperation
                setattr(c, f, v)
            except (InvalidOperation, ValueError):
                errors[f] = 'Enter a price from 0 to 10,000 SAR.'
    for f in ('starts_on', 'ends_on'):
        if f in data:
            raw = data.get(f)
            if raw in (None, ''):
                setattr(c, f, None)
            else:
                d = passes.parse_day(raw)
                if d is None:
                    errors[f] = 'Use a date.'
                setattr(c, f, d)
    if c.starts_on and c.ends_on and c.ends_on < c.starts_on:
        errors['ends_on'] = 'The last day must be on or after the first day.'
    if (c.full_price or 0) > 0 and not c.ends_on:
        errors.setdefault('ends_on', 'Set the class’s last day to sell it as a whole class.')


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def admin_classes(request):
    """GET /api/classes/admin/?day=&q=&include_paused=1 · POST {name, category, day_of_week, start_time, …}"""
    if request.method == 'GET':
        qs = GymClass.objects.all() if request.query_params.get('include_paused') else GymClass.objects.filter(is_active=True)
        if request.query_params.get('day', '').isdigit():
            qs = qs.filter(day_of_week=int(request.query_params['day']))
        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(trainer__user__first_name__icontains=q) | Q(location__icontains=q))
        return Response([_row(c) for c in _annotated(qs).order_by('day_of_week', 'start_time')])
    c = GymClass(category='strength', level='all_levels')
    errors = {}
    _apply(c, {'name': '', 'day_of_week': None, 'start_time': None, **request.data}, errors)
    if not errors:
        _conflicts(c, errors)
    if errors:
        return Response(errors, status=400)
    c.save()
    return Response(_row(c), status=201)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def admin_class_detail(request, class_id):
    """PATCH edits (capacity up → waitlist moves in) · DELETE removes it and tells booked members."""
    c = get_object_or_404(GymClass.objects.select_related('trainer__user'), id=class_id)
    if request.method == 'DELETE':
        if ClassPass.objects.filter(gym_class=c, is_cancelled=False, valid_until__gte=timezone.localdate()).exists():
            return Response({'detail': 'Members have paid for upcoming sessions of this class — pause it and refund '
                                       'them first, or let its run finish.'}, status=400)
        if ClassPass.objects.filter(gym_class=c).exists():
            return Response({'detail': 'This class has payment history — pause it instead of deleting it.'}, status=400)
        for e in c.enrollments.filter(status__in=BOOKED + ['waitlist']).select_related('member'):
            notify(e.member, 'attendance', 'Class cancelled',
                   f'{c.name} on {DAYS[c.day_of_week]}s at {c.start_time:%H:%M} has been removed from the timetable.',
                   icon='event_busy', action_label='Find another class', action_path='/dashboard/classes')
        _tell_team(c, 'Class cancelled', f'{request.user.full_name or request.user.email} removed {c.name} '
                   f'({DAYS[c.day_of_week]}s at {c.start_time:%H:%M}) from the timetable.')
        c.delete()
        return Response(status=204)
    before = (c.day_of_week, c.start_time, c.is_active)
    errors = {}
    _apply(c, request.data, errors)
    if not errors and 'capacity' in request.data:
        booked = c.enrollments.filter(status__in=BOOKED).count()
        if c.capacity < booked:
            errors['capacity'] = f'{booked} members are booked — remove some from the roster first.'
    if not errors:
        _conflicts(c, errors)
    if errors:
        return Response(errors, status=400)
    c.save()
    _promote_waitlist(c)
    if (c.day_of_week, c.start_time, c.is_active) != before:
        what = (f'{c.name} is paused for now.' if not c.is_active else
                f'{c.name} now runs on {DAYS[c.day_of_week]}s at {c.start_time:%H:%M}.')
        for e in c.enrollments.filter(status__in=BOOKED + ['waitlist']).select_related('member'):
            notify(e.member, 'attendance', 'Class timetable changed', what, icon='event_note',
                   action_label='My classes', action_path='/dashboard/classes')
        _tell_team(c, 'Class paused' if not c.is_active else 'Class timetable changed', what, staff=False)
    return Response(_row(_annotated(GymClass.objects.filter(id=c.id)).get()))


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_class_roster(request, class_id):
    c = get_object_or_404(GymClass, id=class_id)
    return Response(roster(c))


def roster(c):
    """Standing spots + waitlist, and (paid classes) upcoming single-session bookings by date."""
    rows = (c.enrollments.filter(status__in=BOOKED + ['waitlist']).select_related('member')
            .order_by('status', 'enrolled_at'))
    singles = (ClassPass.objects.filter(gym_class=c, kind='session', is_cancelled=False,
                                        session_date__gte=timezone.localdate())
               .select_related('member').order_by('session_date'))
    return {'class': _row(c), 'roster': [{
        'id': e.id, 'member_id': e.member_id, 'name': e.member.full_name or e.member.email, 'email': e.member.email,
        'status': 'waitlist' if e.status == 'waitlist' else 'booked', 'since': e.enrolled_at,
    } for e in rows], 'sessions': [{
        'id': p.id, 'member_id': p.member_id, 'name': p.member.full_name or p.member.email, 'email': p.member.email,
        'date': p.session_date, 'amount': str(p.amount),
    } for p in singles]}


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_roster_remove(request, class_id, enrollment_id):
    e = get_object_or_404(ClassEnrollment.objects.select_related('gym_class', 'member'), id=enrollment_id, gym_class_id=class_id)
    was_booked = e.status in BOOKED
    e.status = 'cancelled'
    e.save(update_fields=['status'])
    notify(e.member, 'attendance', 'Class booking removed',
           f'The gym removed your booking for {e.gym_class.name}. Contact the front desk with any questions.',
           icon='event_busy', action_label='My classes', action_path='/dashboard/classes')
    if was_booked:
        _promote_waitlist(e.gym_class)
    _tell_team(e.gym_class, 'Class booking cancelled',
               f'{e.member.full_name or e.member.email} was removed from {e.gym_class.name} by '
               f'{request.user.full_name or request.user.email}.')
    return Response({'detail': f'{e.member.full_name or e.member.email} removed.'})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def classes_schedule(request):
    """GET /api/classes/schedule/ — active classes grouped by start time × day (Mon–Sun)."""
    slots = {}
    for c in _annotated(GymClass.objects.filter(is_active=True)):
        key = c.start_time.strftime('%H:%M')
        slots.setdefault(key, {'time': key})[DAYS[c.day_of_week]] = _row(c)
    return Response({'days': DAYS, 'slots': [slots[k] for k in sorted(slots)]})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def classes_stats(request):
    """GET /api/classes/stats/ — fill rate = booked spots ÷ capacity across active classes."""
    active = list(_annotated(GymClass.objects.filter(is_active=True)))
    capacity = sum(c.capacity for c in active)
    booked = sum(c.booked for c in active)
    return Response({
        'total_classes': len(active),
        'paused': GymClass.objects.filter(is_active=False).count(),
        'active_enrollments': booked,
        'fill_rate': round(booked / capacity * 100, 1) if capacity else 0,
        'full_classes': sum(c.booked >= c.capacity for c in active),
        'waitlisted': sum(c.waiting for c in active),
        'members_booked': ClassEnrollment.objects.filter(status__in=BOOKED, gym_class__is_active=True)
        .values('member').distinct().count(),
    })
