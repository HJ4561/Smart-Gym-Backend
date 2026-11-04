"""
The coach portal: everything a trainer (role "trainer") manages for themselves.

  /api/coach/me/                  profile + numbers (GET/PATCH), photo (POST me/photo/)
  /api/coach/hours/               weekly 1-on-1 hours and booking switches (GET/PUT)
  /api/coach/sessions/            their 1-on-1 sessions; POST <id>/status/ completed|no_show|cancelled
  /api/coach/courses/             their courses (drafts → sent for review → published by the gym)
  /api/coach/students/            members in their courses and their 1-on-1 clients
  /api/coach/classes/             group classes they teach, with rosters
  /api/coach/machines/ · videos/  Machine Guide videos (Basic free · Advanced · Premium)
  /api/coach/reviews/ · earnings/

Pricing of 1-on-1 sessions, verification and publishing stay with the gym (admin portal).
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.admin_api import catalog_ops, ops
from apps.catalog.models import Course, Enrollment, TrainerProfile, TrainerReview, TrainerSession
from apps.classes.models import ClassPass, GymClass
from apps.notifications.service import notify_staff
from apps.workouts import videos as vids
from apps.workouts.models import Exercise, ExerciseVideo, VideoPurchase

logger = logging.getLogger(__name__)


class IsCoach(BasePermission):
    message = 'The coach portal is for coaches.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == 'trainer')


def _me(request):
    """The signed-in coach's profile (made on first visit if the gym hasn't set one up yet)."""
    t, _ = TrainerProfile.objects.select_related('user').get_or_create(
        user=request.user, defaults={'title': 'Coach', 'hourly_rate': 0, 'is_accepting_clients': False})
    return t


def _f(v):
    return round(float(v or 0), 2)


# ============================================================
# PROFILE + DASHBOARD
# ============================================================
COACH_EDITABLE = ('first_name', 'last_name', 'phone', 'title', 'title_ar', 'credentials', 'bio', 'bio_ar', 'specialties',
                  'years_experience', 'is_accepting_clients', 'offers_1on1')


def _earnings(t, since=None):
    def window(qs, field):
        return qs.filter(**{f'{field}__gte': since}) if since else qs
    sessions = window(TrainerSession.objects.filter(trainer=t, status__in=('scheduled', 'completed')), 'created_at')
    courses = window(Enrollment.objects.filter(course__trainer=t).exclude(status='refunded'), 'enrolled_at')
    videos = window(VideoPurchase.objects.filter(video__coach=t), 'created_at')
    classes = window(ClassPass.objects.filter(gym_class__trainer=t, is_cancelled=False), 'created_at')
    parts = {
        'sessions': _f(sessions.aggregate(s=Sum('amount_charged'))['s']),
        'courses': _f(courses.aggregate(s=Sum('amount_paid'))['s']),
        'videos': _f(videos.aggregate(s=Sum('amount'))['s']),
        'classes': _f(classes.aggregate(s=Sum('amount'))['s']),
    }
    return {**parts, 'total': round(sum(parts.values()), 2)}


def _profile_row(t):
    u = t.user
    now = timezone.now()
    row = catalog_ops._trainer_row(t, catalog_ops._trainer_counts([t.id]))
    students = (Enrollment.objects.filter(course__trainer=t, status='active').values('member').distinct().count())
    row.update({
        'avatar': u.avatar.url if u.avatar else None,
        'offers_consultation': t.offers_consultation,
        'stats': {
            'upcoming_sessions': t.sessions.filter(status='scheduled', scheduled_at__gte=now).count(),
            'sessions_this_month': t.sessions.filter(status='completed',
                                                     scheduled_at__gte=now.replace(day=1, hour=0, minute=0)).count(),
            'active_students': students,
            'courses_published': t.courses.filter(status='published').count(),
            'courses_in_review': t.courses.filter(status='pending').count(),
            'courses_draft': t.courses.filter(status='draft').count(),
            'classes': GymClass.objects.filter(trainer=t, is_active=True).count(),
            'videos': ExerciseVideo.objects.filter(coach=t, is_active=True).count(),
            'rating': _f(t.rating), 'reviews': t.total_reviews,
            'earnings_30d': _earnings(t, now - timedelta(days=30))['total'],
        },
    })
    return row


@api_view(['GET', 'PATCH'])
@permission_classes([IsCoach])
def me(request):
    """GET /api/coach/me/ · PATCH name, phone, title, credentials, bio, specialties, years, booking switches."""
    t = _me(request)
    if request.method == 'PATCH':
        data = {k: v for k, v in request.data.items() if k in COACH_EDITABLE}
        errors = {}
        catalog_ops._apply_trainer(t, t.user, data, errors)
        if errors:
            return Response(errors, status=400)
        with transaction.atomic():
            t.user.save()
            t.save()
    return Response(_profile_row(t))


@api_view(['POST'])
@permission_classes([IsCoach])
@parser_classes([MultiPartParser, FormParser])
def me_photo(request):
    """POST /api/coach/me/photo/ multipart {image} — the photo members see."""
    t = _me(request)
    url, err = catalog_ops._save_image(request, 'trainers')
    if err:
        return Response({'image': [err]}, status=400)
    t.photo_url = url
    t.save(update_fields=['photo_url', 'updated_at'])
    return Response(_profile_row(t))


@api_view(['GET'])
@permission_classes([IsCoach])
def dashboard(request):
    """GET /api/coach/dashboard/ — today at a glance: next sessions, today's classes, course reviews, numbers."""
    t = _me(request)
    now = timezone.now()
    today = timezone.localdate()
    upcoming = (t.sessions.filter(status='scheduled', scheduled_at__gte=now - timedelta(hours=1))
                .select_related('member', 'trainer__user').order_by('scheduled_at')[:6])
    classes = GymClass.objects.filter(trainer=t, is_active=True, day_of_week=today.weekday()).exclude(ends_on__lt=today)
    from apps.classes.views import _annotated, _row as class_row
    recent = (Enrollment.objects.filter(course__trainer=t).select_related('member', 'course').order_by('-enrolled_at')[:6])
    return Response({
        'profile': _profile_row(t),
        'next_sessions': [ops._session_row(s) for s in upcoming],
        'classes_today': [class_row(c) for c in _annotated(classes).order_by('start_time')],
        'recent_students': [{'member': e.member.full_name or e.member.email, 'course': e.course.title,
                             'course_id': e.course_id, 'enrolled_at': e.enrolled_at} for e in recent],
        'earnings': {'last_30_days': _earnings(t, now - timedelta(days=30)), 'all_time': _earnings(t)},
    })


# ============================================================
# HOURS + 1-ON-1 SESSIONS
# ============================================================
@api_view(['GET', 'PUT'])
@permission_classes([IsCoach])
def hours(request):
    """
    GET /api/coach/hours/ · PUT {windows: [{day_of_week, start_time, end_time}], is_accepting_clients, offers_1on1}
    The session price is set by the gym.
    """
    t = _me(request)
    if request.method == 'PUT':
        errors = ops.save_hours(t, request.data, errors={}, allow_rate=False)
        if errors:
            return Response(errors, status=400)
    return Response(ops._trainer_row(t))


@api_view(['GET'])
@permission_classes([IsCoach])
def sessions(request):
    """GET /api/coach/sessions/?scope=upcoming|past"""
    t = _me(request)
    qs = TrainerSession.objects.select_related('trainer__user', 'member').filter(trainer=t).exclude(status='pending_payment')
    now = timezone.now()
    if request.query_params.get('scope', 'upcoming') == 'past':
        qs = qs.filter(scheduled_at__lt=now).order_by('-scheduled_at')
    else:
        qs = qs.filter(scheduled_at__gte=now - timedelta(hours=2)).order_by('scheduled_at')
    return Response([ops._session_row(s) for s in qs[:200]])


@api_view(['POST'])
@permission_classes([IsCoach])
def session_status(request, session_id):
    """POST /api/coach/sessions/<id>/status/ {status: completed|no_show|cancelled} — cancelling refunds the member."""
    t = _me(request)
    row, error = ops.change_session_status(session_id, request.data.get('status'),
                                           by=t.user.full_name or 'your coach', trainer_id=t.id)
    return Response(error, status=400) if error else Response(row)


# ============================================================
# COURSES (drafts → review → published by the gym)
# ============================================================
COACH_COURSE_FIELDS = ('title', 'title_ar', 'subtitle', 'subtitle_ar', 'description', 'description_ar', 'category',
                       'level', 'duration_weeks', 'days_per_week',
                       'estimated_minutes_per_session', 'price', 'promo_video_url')
LOCKED_WHEN_PUBLISHED = ('price', 'title', 'title_ar')


def _course(request, course_id):
    return get_object_or_404(Course.objects.select_related('trainer__user'), id=course_id, trainer=_me(request))


def _coach_course_row(c, modules=False):
    row = catalog_ops._course_row(c, modules=modules)
    row['review_note'] = c.review_note
    row['submitted_at'] = c.submitted_at
    return row


@api_view(['GET', 'POST'])
@permission_classes([IsCoach])
@parser_classes([JSONParser])
def courses(request):
    """GET my courses · POST {title, description, category, …, modules?} → a new draft."""
    t = _me(request)
    if request.method == 'GET':
        qs = (Course.objects.filter(trainer=t).select_related('trainer__user')
              .annotate(enrolled=Count('enrollments', distinct=True), module_count=Count('modules', distinct=True))
              .order_by('-updated_at'))
        return Response({'courses': [_coach_course_row(c) for c in qs], 'categories': catalog_ops.COURSE_CATEGORIES})
    data = {k: v for k, v in request.data.items() if k in COACH_COURSE_FIELDS}
    c = Course(status='draft', trainer=t)
    errors = {}
    catalog_ops._apply_course(c, {'title': '', 'description': '', 'category': '', **data}, errors)
    if errors:
        return Response(errors, status=400)
    with transaction.atomic():
        c.save()
        if 'modules' in request.data:
            catalog_ops._replace_modules(c, request.data['modules'], errors)
            if errors:
                transaction.set_rollback(True)
                return Response(errors, status=400)
    return Response(_coach_course_row(c, modules=True), status=201)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsCoach])
@parser_classes([JSONParser])
def course_detail(request, course_id):
    """
    GET with lessons · PATCH fields and/or `modules` (lessons) · DELETE a course nobody joined.
    Published courses keep their title and price — ask the gym to change those.
    """
    c = _course(request, course_id)
    if request.method == 'GET':
        return Response(_coach_course_row(c, modules=True))
    if request.method == 'DELETE':
        if Enrollment.objects.filter(course=c).exists():
            return Response({'detail': 'Members are enrolled in this course, so it can’t be deleted.'}, status=400)
        c.delete()
        return Response(status=204)
    data = {k: v for k, v in request.data.items() if k in COACH_COURSE_FIELDS}
    errors = {}
    if c.status == 'published':
        for f in LOCKED_WHEN_PUBLISHED:
            if f in data and str(data[f]).strip() != str(getattr(c, f)).strip():
                if f == 'price' and catalog_ops._f(data[f]) == catalog_ops._f(c.price):
                    continue
                errors[f] = f'The {f} of a published course is changed by the gym — message them.'
    if not errors:
        catalog_ops._apply_course(c, data, errors)
    if not errors and c.status == 'published' and 'modules' in request.data and not request.data['modules']:
        errors['modules'] = 'A published course needs at least one lesson.'
    if errors:
        return Response(errors, status=400)
    with transaction.atomic():
        c.save()
        if 'modules' in request.data:
            catalog_ops._replace_modules(c, request.data['modules'], errors)
            if errors:
                transaction.set_rollback(True)
                return Response(errors, status=400)
    return Response(_coach_course_row(c, modules=True))


@api_view(['POST'])
@permission_classes([IsCoach])
def course_submit(request, course_id):
    """POST /api/coach/courses/<id>/submit/ — send a draft to the gym for review (they publish it)."""
    c = _course(request, course_id)
    if c.status not in ('draft', 'archived'):
        return Response({'detail': 'Only drafts can be sent for review.' if c.status != 'pending'
                         else 'This course is already waiting for review.'}, status=400)
    errors = {}
    if not (c.description or '').strip():
        errors['description'] = 'Add a description first.'
    if not c.modules.exists():
        errors['modules'] = 'Add at least one lesson first.'
    if errors:
        return Response(errors, status=400)
    c.status = 'pending'
    c.submitted_at = timezone.now()
    c.review_note = ''
    c.save(update_fields=['status', 'submitted_at', 'review_note', 'updated_at'])
    from apps.core.i18n import ar, bi
    coach = c.trainer.user.full_name
    notify_staff('course', bi('Course waiting for review', 'دورة بانتظار المراجعة'),
                 bi(f'{coach} sent “{c.title}” for review.', f'أرسل {coach} «{ar(c, "title")}» للمراجعة.'),
                 icon='rate_review',
                 action_label='Review course', action_path='/admin/trainers?tab=courses&status=pending')
    return Response(_coach_course_row(c, modules=True))


@api_view(['POST'])
@permission_classes([IsCoach])
@parser_classes([MultiPartParser, FormParser])
def course_cover(request, course_id):
    """POST /api/coach/courses/<id>/cover/ multipart {image}."""
    c = _course(request, course_id)
    url, err = catalog_ops._save_image(request, 'courses')
    if err:
        return Response({'image': [err]}, status=400)
    c.cover_image_url = url
    c.save(update_fields=['cover_image_url', 'updated_at'])
    return Response(_coach_course_row(c, modules=True))


# ============================================================
# STUDENTS, CLASSES, REVIEWS, EARNINGS
# ============================================================
@api_view(['GET'])
@permission_classes([IsCoach])
def students(request):
    """GET /api/coach/students/ — course enrolments (with progress) and 1-on-1 clients."""
    t = _me(request)
    enrolments = (Enrollment.objects.filter(course__trainer=t).select_related('member', 'course')
                  .annotate(done=Count('completed_modules', distinct=True), total=Count('course__modules', distinct=True))
                  .order_by('-enrolled_at'))
    clients = (TrainerSession.objects.filter(trainer=t).exclude(status__in=('pending_payment', 'cancelled'))
               .values('member_id', 'member__first_name', 'member__last_name', 'member__email')
               .annotate(sessions=Count('id'), completed=Count('id', filter=Q(status='completed'))).order_by('-sessions'))
    return Response({
        'enrolments': [{
            'id': e.id, 'member_id': e.member_id, 'name': e.member.full_name or e.member.email, 'email': e.member.email,
            'course_id': e.course_id, 'course': e.course.title, 'status': e.status, 'enrolled_at': e.enrolled_at,
            'completed_days': e.done, 'total_days': e.total,
            'progress_percent': int(e.done * 100 / e.total) if e.total else 0, 'amount_paid': str(e.amount_paid),
        } for e in enrolments],
        'clients': [{
            'member_id': r['member_id'], 'email': r['member__email'],
            'name': f"{r['member__first_name']} {r['member__last_name']}".strip() or r['member__email'],
            'sessions': r['sessions'], 'completed': r['completed'],
        } for r in clients],
    })


@api_view(['GET'])
@permission_classes([IsCoach])
def classes(request):
    """GET /api/coach/classes/ — the group classes they teach, each with its roster."""
    from apps.classes.views import _annotated, roster
    t = _me(request)
    qs = _annotated(GymClass.objects.filter(trainer=t)).order_by('-is_active', 'day_of_week', 'start_time')
    return Response([roster(c) for c in qs])


@api_view(['GET'])
@permission_classes([IsCoach])
def reviews(request):
    """GET /api/coach/reviews/"""
    t = _me(request)
    qs = TrainerReview.objects.filter(trainer=t).select_related('member').order_by('-created_at')[:100]
    return Response({'rating': _f(t.rating), 'total': t.total_reviews, 'reviews': [{
        'id': r.id, 'rating': r.rating, 'comment': r.comment, 'created_at': r.created_at,
        'member': (r.member.first_name or 'Member') + (f' {r.member.last_name[:1]}.' if r.member.last_name else ''),
    } for r in qs]})


@api_view(['GET'])
@permission_classes([IsCoach])
def earnings(request):
    """GET /api/coach/earnings/ — what members paid for their sessions, courses, videos and classes."""
    from dateutil.relativedelta import relativedelta
    t = _me(request)
    now = timezone.now()
    this_month = timezone.localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = []
    for back in range(5, -1, -1):           # the last 6 calendar months
        start = this_month - relativedelta(months=back)
        end = start + relativedelta(months=1)
        e_after = _earnings(t, end)['total'] if end <= now else 0
        months.append({'month': start.strftime('%Y-%m'), 'total': round(_earnings(t, start)['total'] - e_after, 2)})
    return Response({'last_30_days': _earnings(t, now - timedelta(days=30)), 'all_time': _earnings(t),
                     'by_month': months})


# ============================================================
# MACHINE GUIDE VIDEOS
# ============================================================
@api_view(['GET'])
@permission_classes([IsCoach])
def machines(request):
    """GET /api/coach/machines/?q= — machines to add videos to, with how many each has."""
    _me(request)
    qs = Exercise.objects.filter(is_active=True).annotate(
        video_count=Count('videos', filter=Q(videos__is_active=True), distinct=True)).order_by('name')
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(machine_id__icontains=q) | Q(equipment_type__icontains=q))
    return Response([{'id': e.id, 'name': e.name, 'machine_id': e.machine_id, 'category': e.category,
                      'primary_muscle': e.primary_muscle, 'image_url': e.image_url, 'video_count': e.video_count,
                      'has_walkthrough': bool(e.video_url)} for e in qs])


@api_view(['GET', 'POST'])
@permission_classes([IsCoach])
@parser_classes([JSONParser])
def videos(request):
    """GET my videos · POST {exercise_id, tier, title, video_url, duration_minutes, price, description}."""
    t = _me(request)
    if request.method == 'GET':
        qs = vids.with_sales(ExerciseVideo.objects.filter(coach=t)).order_by('exercise__name', 'created_at')
        return Response([vids.coach_row(v) for v in qs])
    ex = Exercise.objects.filter(id=request.data.get('exercise_id'), is_active=True).first()
    errors = {}
    if ex is None:
        errors['exercise_id'] = 'Choose a machine.'
    v = ExerciseVideo(exercise=ex, coach=t, tier='basic')
    vids.apply(v, {'title': '', 'video_url': '', 'tier': 'basic', 'price': 0, **request.data}, errors)
    if errors:
        return Response(errors, status=400)
    v.save()
    return Response(vids.coach_row(v), status=201)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsCoach])
@parser_classes([JSONParser])
def video_detail(request, video_id):
    """PATCH any field (or is_active to hide it) · DELETE one nobody has bought (hide it otherwise)."""
    t = _me(request)
    v = get_object_or_404(ExerciseVideo.objects.select_related('exercise', 'coach__user'), id=video_id, coach=t)
    if request.method == 'DELETE':
        if v.purchases.exists():
            return Response({'detail': 'Members have bought this video — hide it instead, so they keep access.'}, status=400)
        v.delete()
        return Response(status=204)
    errors = {}
    if v.purchases.exists() and 'tier' in request.data and request.data['tier'] != v.tier:
        errors['tier'] = 'Members bought this video, so its tier can’t change. Add a new video instead.'
    if not errors:
        vids.apply(v, request.data, errors)
    if errors:
        return Response(errors, status=400)
    v.save()
    return Response(vids.coach_row(v))
