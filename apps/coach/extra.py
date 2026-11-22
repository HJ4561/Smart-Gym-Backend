"""
More of the coach portal:

  GET  /api/coach/students/<member_id>/          one student: membership, course progress, sessions, classes,
                                                 gym visits, workouts and personal records
  GET  /api/coach/classes/?scope=upcoming|past   their classes with dated sessions (next 4 / last 8) and posts
  GET/POST /api/coach/classes/<id>/posts/        a message (reaches the class at once) or a video (after an admin
                                                 approves it) for the class or one of its dates
  DELETE   /api/coach/classes/<id>/posts/<post>/
  POST /api/coach/sessions/<id>/change/          move, delay or edit a 1-on-1 (the member is told)
  POST /api/coach/videos/<id>/thumbnail/         the still members see before playing
"""
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.accounts.models import User
from apps.admin_api import catalog_ops, ops
from apps.approvals.registry import pending_for, submit_create, submit_update
from apps.catalog.models import Enrollment, TrainerSession
from apps.classes.models import ClassEnrollment, ClassPass, ClassPost, GymClass
from apps.core.i18n import ar, bi
from apps.notifications.service import notify
from apps.workouts import videos as vids
from apps.workouts.models import ExerciseVideo

from .views import IsCoach, _me

BOOKED = ('enrolled', 'attended')
SENT = 'Sent to an admin for approval. Members keep seeing the current version until then.'


# ============================================================
# ONE STUDENT
# ============================================================
@api_view(['GET'])
@permission_classes([IsCoach])
def student(request, member_id):
    t = _me(request)
    member = get_object_or_404(User, id=member_id)
    enrolments = (Enrollment.objects.filter(course__trainer=t, member=member).select_related('course')
                  .prefetch_related('completed_modules', 'course__modules'))
    sessions = TrainerSession.objects.filter(trainer=t, member=member).exclude(status='pending_payment')
    in_classes = ClassEnrollment.objects.filter(gym_class__trainer=t, member=member).select_related('gym_class')
    if not (enrolments.exists() or sessions.exists() or in_classes.exists()):
        raise Http404          # only their own students
    from apps.members.models import CheckIn, Membership
    from apps.workouts.models import PersonalRecord, Workout
    now = timezone.now()
    m = Membership.objects.select_related('plan').filter(member=member).first()
    visits = CheckIn.objects.filter(member=member)
    workouts = Workout.objects.filter(member=member, status='completed')
    month = now - timedelta(days=30)
    last_visit = visits.order_by('-check_in_time').first()
    courses = []
    for e in enrolments:
        lessons = list(e.course.modules.all().order_by('order'))
        done = {x.id for x in e.completed_modules.all()}
        courses.append({
            'course_id': e.course_id, 'course': e.course.title, 'status': e.status, 'enrolled_at': e.enrolled_at,
            'completed_at': e.completed_at, 'done': len(done), 'total': len(lessons),
            'progress_percent': int(len(done) * 100 / len(lessons)) if lessons else 0,
            'lessons': [{'id': x.id, 'title': x.title, 'week': x.week_number, 'day': x.day_number, 'done': x.id in done}
                        for x in lessons],
        })
    agg = workouts.filter(completed_at__gte=month).aggregate(n=Count('id'), sets=Sum('total_sets'),
                                                              tonnage=Sum('total_tonnage_kg'), minutes=Sum('duration_minutes'))
    return Response({
        'member': {'id': member.id, 'name': member.full_name or member.email, 'email': member.email,
                   'phone': member.phone or '', 'avatar': member.avatar.url if member.avatar else None,
                   'joined': member.date_joined},
        'membership': {'plan': m.plan.display_name, 'status': m.status, 'expiry_date': m.expiry_date,
                       'days_remaining': m.days_remaining} if m else None,
        'courses': courses,
        'sessions': [ops._session_row(s) for s in sessions.select_related('trainer__user', 'member').order_by('-scheduled_at')[:50]],
        'classes': [{'class_id': c.gym_class_id, 'name': c.gym_class.name, 'day': c.gym_class.day_of_week,
                     'start_time': c.gym_class.start_time.strftime('%H:%M'), 'status': c.status} for c in in_classes],
        'insights': {
            'visits_30d': visits.filter(check_in_time__gte=month).count(),
            'visits_total': visits.count(),
            'last_visit': last_visit.check_in_time if last_visit else None,
            'days_since_visit': ((timezone.localdate() - timezone.localtime(last_visit.check_in_time).date()).days
                                 if last_visit else None),
            'workouts_30d': agg['n'] or 0,
            'sets_30d': agg['sets'] or 0,
            'tonnage_30d_kg': float(agg['tonnage'] or 0),
            'minutes_30d': agg['minutes'] or 0,
            'sessions_completed': sessions.filter(status='completed').count(),
            'sessions_missed': sessions.filter(status='no_show').count(),
            'upcoming_sessions': sessions.filter(status='scheduled', scheduled_at__gte=now).count(),
        },
        'records': [{'exercise': r.exercise.name, 'best_weight_kg': float(r.best_weight_kg), 'best_reps': r.best_reps,
                     'estimated_1rm': float(r.best_estimated_1rm), 'achieved_at': r.achieved_at}
                    for r in PersonalRecord.objects.filter(member=member).select_related('exercise')
                    .order_by('-achieved_at')[:10]],
        'recent_workouts': [{'id': w.id, 'name': w.name, 'completed_at': w.completed_at, 'sets': w.total_sets,
                             'tonnage_kg': float(w.total_tonnage_kg), 'minutes': w.duration_minutes}
                            for w in workouts.order_by('-completed_at')[:8]],
    })


# ============================================================
# CLASSES: dates (upcoming / past) and posts
# ============================================================
def _dates(c, scope, today):
    """The class's dated sessions: the next 4 or the last 8, inside its first/last day."""
    out, step = [], (1 if scope == 'upcoming' else -1)
    d = today
    shift = (c.day_of_week - d.weekday()) % 7 if step > 0 else -((d.weekday() - c.day_of_week) % 7)
    d = d + timedelta(days=shift)
    if step < 0 and d == today:
        d -= timedelta(days=7)          # today's session counts as upcoming
    created = timezone.localtime(c.created_at).date()
    limit = 4 if step > 0 else 8
    for _ in range(60):
        if len(out) >= limit:
            break
        if (c.ends_on and d > c.ends_on and step > 0) or (step < 0 and (d < created or (c.starts_on and d < c.starts_on))):
            break
        if not (c.starts_on and d < c.starts_on) and not (c.ends_on and d > c.ends_on):
            out.append(d)
        d += timedelta(days=7 * step)
    return out


def _post_row(p):
    return {'id': p.id, 'class_id': p.gym_class_id, 'kind': p.kind, 'for_date': p.for_date, 'title': p.title,
            'body': p.body, 'video_url': p.video_url, 'thumbnail_url': p.thumbnail_url or _yt_thumb(p.video_url),
            'approval_status': p.approval_status, 'created_at': p.created_at,
            'author': (p.author.full_name or p.author.email) if p.author_id else ''}


def _yt_thumb(url):
    yid = vids.youtube_id(url)
    return f'https://img.youtube.com/vi/{yid}/hqdefault.jpg' if yid else None


@api_view(['GET'])
@permission_classes([IsCoach])
def classes(request):
    """GET /api/coach/classes/?scope=upcoming|past — each class with its roster, dated sessions and posts."""
    from apps.classes.views import _annotated, roster
    t = _me(request)
    scope = 'past' if request.query_params.get('scope') == 'past' else 'upcoming'
    today = timezone.localdate()
    qs = _annotated(GymClass.objects.filter(trainer=t)).order_by('-is_active', 'day_of_week', 'start_time')
    if scope == 'upcoming':
        qs = qs.filter(is_active=True).exclude(ends_on__lt=today)
    out = []
    for c in qs:
        row = roster(c)
        dates = _dates(c, scope, today)
        singles = dict(ClassPass.objects.filter(gym_class=c, kind='session', is_cancelled=False, session_date__in=dates)
                       .values_list('session_date').annotate(n=Count('id')))
        posts = list(c.posts.select_related('author').order_by('-created_at')[:30])
        row['dates'] = [{'date': d, 'drop_ins': singles.get(d, 0),
                         'posts': [_post_row(p) for p in posts if p.for_date == d]} for d in dates]
        row['posts'] = [_post_row(p) for p in posts if p.for_date is None]
        if scope == 'past' and not dates:
            continue
        out.append(row)
    return Response(out)


def _audience(c, for_date=None):
    """Members to tell: everyone with a standing spot, plus (for one date) whoever bought that session."""
    ids = set(c.enrollments.filter(status__in=BOOKED).values_list('member_id', flat=True))
    passes = ClassPass.objects.filter(gym_class=c, is_cancelled=False)
    passes = passes.filter(Q(kind='full') | Q(session_date=for_date)) if for_date else passes.filter(kind='full')
    ids |= set(passes.values_list('member_id', flat=True))
    return User.objects.filter(id__in=ids, is_active=True)


def tell_class(post):
    c = post.gym_class
    when = bi(f' ({post.for_date:%d %b})', f' ({post.for_date:%d/%m})') if post.for_date else bi('', '')
    for m in _audience(c, post.for_date):
        notify(m, 'attendance', bi(f'{c.name}: {post.title}', f'{ar(c, "name")}: {post.title}') + when,
               bi(post.body[:300] or 'Your coach shared a video for this class.',
                  post.body[:300] or 'شارك مدربك فيديو لهذه الحصة.'),
               icon='smart_display' if post.kind == 'video' else 'campaign',
               action_label='Open', action_path='/dashboard/classes')


@api_view(['GET', 'POST'])
@permission_classes([IsCoach])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def class_posts(request, class_id):
    """
    GET the class's posts · POST {kind: message|video, title, body, video_url?, for_date?, image?}
    A message reaches the class straight away; a video goes to an admin first.
    """
    t = _me(request)
    c = get_object_or_404(GymClass, id=class_id, trainer=t)
    if request.method == 'GET':
        return Response([_post_row(p) for p in c.posts.select_related('author')])
    d = request.data
    errors = {}
    kind = d.get('kind') if d.get('kind') in ('message', 'video') else None
    if kind is None:
        errors['kind'] = 'Choose a message or a video.'
    title = ' '.join(str(d.get('title') or '').split())
    if not 3 <= len(title) <= 120:
        errors['title'] = 'Title must be 3–120 characters.'
    body = str(d.get('body') or '').strip()[:2000]
    if kind == 'message' and not body:
        errors['body'] = 'Write the message.'
    url = str(d.get('video_url') or '').strip()
    if kind == 'video' and (not url.startswith('https://') or len(url) > 300):
        errors['video_url'] = 'Paste the full video link (https://…, e.g. YouTube or Vimeo).'
    for_date = None
    if d.get('for_date'):
        from django.utils.dateparse import parse_date
        for_date = parse_date(str(d['for_date']))
        if for_date is None or for_date.weekday() != c.day_of_week:
            errors['for_date'] = 'Choose one of this class’s dates.'
    thumb = ''
    if request.FILES.get('image'):
        thumb, err = catalog_ops._save_image(request, 'classes')
        if err:
            errors['image'] = err
    if errors:
        return Response(errors, status=400)
    p = ClassPost.objects.create(gym_class=c, author=request.user, kind=kind, title=title, body=body,
                                 video_url=url if kind == 'video' else '', thumbnail_url=thumb or '', for_date=for_date,
                                 approval_status='pending' if kind == 'video' else 'approved')
    if kind == 'video':
        submit_create(request.user, 'coach.class_video', p)
        return Response({**_post_row(p), 'detail': 'Video sent — your class sees it once an admin approves it.'},
                        status=201)
    tell_class(p)
    return Response({**_post_row(p), 'detail': 'Message sent to the class.'}, status=201)


@api_view(['DELETE'])
@permission_classes([IsCoach])
def class_post_detail(request, class_id, post_id):
    p = get_object_or_404(ClassPost, id=post_id, gym_class_id=class_id, gym_class__trainer=_me(request))
    pending_for('coach.class_video', p.pk).update(status='withdrawn')
    p.delete()
    return Response(status=204)


# ============================================================
# 1-ON-1: move, delay, edit
# ============================================================
@api_view(['POST'])
@permission_classes([IsCoach])
def session_change(request, session_id):
    t = _me(request)
    row, error = ops.edit_session(session_id, request.data, by=t.user.full_name or 'your coach', trainer_id=t.id)
    return Response(error, status=400) if error else Response(row)


# ============================================================
# VIDEOS: approval + thumbnail
# ============================================================
def with_pending_video(row):
    cr = pending_for('coach.video', row['id']).filter(kind='update').first()
    row['pending_change'] = cr.payload if cr else None
    return row


def video_change(request, v, data):
    """A coach's edit: their unapproved video changes at once; a live one keeps showing until approved
    (hiding or showing it again is immediate)."""
    errors = {}
    if v.approval_status != 'approved':
        vids.apply(v, data, errors)
        if errors:
            return Response(errors, status=400)
        v.approval_status = 'pending'
        v.save()
        if not pending_for('coach.video', v.pk).filter(kind='create').exists():
            submit_create(request.user, 'coach.video', v)
        return Response({**with_pending_video(vids.coach_row(v)), 'detail': 'Saved — it goes live once an admin approves it.'})
    direct = {k: val for k, val in data.items() if k == 'is_active'}
    rest = {k: val for k, val in data.items() if k != 'is_active'}
    if direct:
        vids.apply(v, direct, errors)
        if errors:
            return Response(errors, status=400)
        v.save()
    cr = None
    if rest:
        cr, errors = submit_update(request.user, 'coach.video', v, rest)
        if errors:
            return Response(errors, status=400)
    row = with_pending_video(vids.coach_row(v))
    return Response({**row, 'detail': SENT}, status=202) if cr else Response(row)


@api_view(['POST'])
@permission_classes([IsCoach])
@parser_classes([MultiPartParser, FormParser])
def video_thumbnail(request, video_id):
    v = get_object_or_404(ExerciseVideo.objects.select_related('exercise', 'coach__user'), id=video_id, coach=_me(request))
    url, err = catalog_ops._save_image(request, 'videos')
    if err:
        return Response({'image': [err]}, status=400)
    return video_change(request, v, {'thumbnail_url': url})
