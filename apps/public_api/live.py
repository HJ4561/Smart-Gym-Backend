"""
GET /api/public/live/ — the real numbers behind the public website (no made-up figures):
who's in the club, today's classes, what was lifted, real member reviews. Cached for a minute.
"""
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Avg, Count, F, Sum
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

CACHE_KEY = 'public-live-v1'
CACHE_SECONDS = 60
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def _short_name(user):
    """'Sara A.' — first name and last initial only, for public reviews."""
    first = (user.first_name or '').strip() or (user.email.split('@')[0] if user.email else 'Member')
    last = (user.last_name or '').strip()
    return f'{first} {last[0]}.' if last else first


def build_live():
    from apps.catalog.models import Course, CourseReview, TrainerProfile, TrainerReview, TrainerSession
    from apps.classes.models import ClassEnrollment, GymClass
    from apps.dashboard.occupancy import current_occupancy
    from apps.members.models import CheckIn, Membership
    from apps.workouts.models import Exercise, WorkoutSet

    now = timezone.now()
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    occ = current_occupancy()

    sets_today = WorkoutSet.objects.filter(created_at__date=today, is_warmup=False)
    volume_today = sets_today.aggregate(v=Sum(F('weight_kg') * F('reps')))['v'] or 0

    memberships = Membership.objects.count()
    active = Membership.objects.filter(status__in=['active', 'expiring', 'frozen']).count()

    reviews = []
    for r in TrainerReview.objects.select_related('member', 'trainer__user').exclude(comment='').filter(rating__gte=4)[:6]:
        reviews.append({'name': _short_name(r.member), 'rating': r.rating, 'comment': r.comment,
                        'about': r.trainer.user.full_name, 'about_ar': r.trainer.user.full_name, 'kind': 'trainer',
                        'created_at': r.created_at})
    for r in CourseReview.objects.select_related('member', 'course').exclude(comment='').filter(rating__gte=4)[:6]:
        reviews.append({'name': _short_name(r.member), 'rating': r.rating, 'comment': r.comment,
                        'about': r.course.title, 'about_ar': r.course.title_ar or r.course.title, 'kind': 'course',
                        'created_at': r.created_at})
    reviews.sort(key=lambda r: r['created_at'], reverse=True)

    ratings = [x for x in (TrainerReview.objects.aggregate(a=Avg('rating'), n=Count('id')),
                           CourseReview.objects.aggregate(a=Avg('rating'), n=Count('id'))) if x['n']]
    total_reviews = sum(x['n'] for x in ratings)
    avg_rating = round(sum(x['a'] * x['n'] for x in ratings) / total_reviews, 1) if total_reviews else None

    classes_today = []
    booked = dict(ClassEnrollment.objects.filter(status__in=['enrolled', 'attended'])
                  .values_list('gym_class').annotate(n=Count('id')))
    for c in GymClass.objects.filter(is_active=True, day_of_week=today.weekday()).order_by('start_time')[:8]:
        classes_today.append({'name': c.name, 'time': c.start_time.strftime('%H:%M'), 'location': c.location,
                              'spots_left': max(0, c.capacity - booked.get(c.id, 0))})

    return {
        'updated_at': now,
        'occupancy': {'current': occ['current'], 'capacity': occ['capacity'], 'percent': occ['percent'],
                      'label': occ['label'], 'peak_window': occ['peak_window']},
        'checkins_today': CheckIn.objects.filter(check_in_time__date=today).count(),
        'volume_today_kg': int(volume_today),
        'sets_30d': WorkoutSet.objects.filter(created_at__gte=now - timedelta(days=30)).count(),
        # 1:1 sessions this week + booked places in this week's coached classes
        'sessions_week': TrainerSession.objects.filter(scheduled_at__date__gte=week_start,
                                                       status__in=['scheduled', 'completed']).count()
        + ClassEnrollment.objects.filter(gym_class__is_active=True, status__in=['enrolled', 'attended']).count(),
        'members_active': active,
        'retention_percent': round(100 * active / memberships) if memberships else None,
        'trainers': TrainerProfile.objects.filter(user__is_active=True).count(),
        'classes_per_week': GymClass.objects.filter(is_active=True).count(),
        'classes_today': classes_today,
        'courses': Course.objects.filter(status='published').count(),
        'machines': Exercise.objects.filter(is_active=True).exclude(qr_tag_id='').count(),
        'avg_rating': avg_rating,
        'reviews_count': total_reviews,
        'reviews': reviews[:6],
    }


@api_view(['GET'])
@permission_classes([AllowAny])
def public_live(request):
    data = cache.get(CACHE_KEY)
    if data is None:
        data = build_live()
        cache.set(CACHE_KEY, data, CACHE_SECONDS)
    return Response(data)
