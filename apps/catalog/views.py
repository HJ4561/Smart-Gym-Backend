from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import timedelta
from django.db import transaction as db_transaction
from django.db.models import Avg, Count, F, Q

from .models import (
    TrainerProfile, TrainerAvailability,
    Course, CourseModule, Enrollment,
    TrainerSession, CourseReview, TrainerReview,
    enrollment_started,
)
from .serializers import (
    TrainerProfileSerializer, TrainerAvailabilitySerializer,
    CourseListSerializer, CourseDetailSerializer, CourseModuleSerializer,
    EnrollmentSerializer, TrainerSessionSerializer,
    CourseReviewSerializer, TrainerReviewSerializer,
)


# ============================================
# TRAINERS
# ============================================
@api_view(['GET'])
@permission_classes([AllowAny])
def trainers_list(request):
    """
    GET /api/catalog/trainers/
    Query params: ?search=&verified=true&accepting=true
    """
    qs = TrainerProfile.objects.select_related('user').filter(user__is_active=True)   # hidden by staff → not listed

    if request.query_params.get('verified') == 'true':
        qs = qs.filter(is_verified=True)
    if request.query_params.get('accepting') == 'true':
        qs = qs.filter(is_accepting_clients=True)

    search = request.query_params.get('search')
    if search:
        qs = qs.filter(user__first_name__icontains=search) | \
             qs.filter(user__last_name__icontains=search) | \
             qs.filter(title__icontains=search)

    return Response(TrainerProfileSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def trainer_detail(request, trainer_id):
    """
    GET /api/catalog/trainers/<id>/
    """
    trainer = get_object_or_404(TrainerProfile, id=trainer_id)
    data = TrainerProfileSerializer(trainer).data
    # Include their courses
    courses = trainer.courses.filter(status='published')
    data['courses'] = CourseListSerializer(courses, many=True, context={'request': request}).data
    data['reviews'] = TrainerReviewSerializer(trainer.reviews.select_related('member')[:20], many=True).data
    if request.user.is_authenticated:
        mine = trainer.reviews.filter(member=request.user).first()
        data['my_review'] = TrainerReviewSerializer(mine).data if mine else None
        data['can_review'], data['review_hint'] = _can_review(request.user, trainer)
    return Response(data)


def _can_review(member, trainer):
    """(allowed, reason) — members review coaches they've trained with (session or course)."""
    trained = TrainerSession.objects.filter(member=member, trainer=trainer).filter(
        Q(status='completed') | Q(status='scheduled', scheduled_at__lte=timezone.now())).exists()
    enrolled = Enrollment.objects.filter(member=member, course__trainer=trainer).exists()
    if trained or enrolled:
        return True, ''
    return False, 'You can review this coach after a session with them or after joining one of their courses.'


def _refresh_trainer_rating(trainer):
    agg = trainer.reviews.aggregate(avg=Avg('rating'), n=Count('id'))
    trainer.rating = round(agg['avg'] or 0, 2)
    trainer.total_reviews = agg['n']
    trainer.save(update_fields=['rating', 'total_reviews'])


# ============================================
# COURSES
# ============================================
@api_view(['GET'])
@permission_classes([AllowAny])
def courses_list(request):
    """
    GET /api/catalog/courses/
    Query params: ?category=Hypertrophy&level=advanced&search=&featured=true
    """
    qs = Course.objects.filter(status='published').select_related('trainer__user')

    category = request.query_params.get('category')
    if category:
        qs = qs.filter(category__iexact=category)

    level = request.query_params.get('level')
    if level:
        qs = qs.filter(level=level)

    if request.query_params.get('featured') == 'true':
        qs = qs.filter(is_featured=True)

    search = request.query_params.get('search')
    if search:
        qs = qs.filter(title__icontains=search) | qs.filter(description__icontains=search)

    return Response(CourseListSerializer(qs, many=True, context={'request': request}).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def course_detail(request, course_id):
    """
    GET /api/catalog/courses/<id>/
    """
    course = get_object_or_404(
        Course.objects.select_related('trainer__user'),
        id=course_id,
        status='published',
    )
    data = CourseDetailSerializer(course, context={'request': request}).data
    # The syllabus is public, the lesson videos and PDFs are for enrolled members only.
    enrolled = (request.user.is_authenticated
                and Enrollment.objects.filter(member=request.user, course=course).exists())
    data['enrolled'] = enrolled
    if not enrolled:
        for m in data['modules']:
            m['video_url'] = ''
            m['pdf_url'] = ''
    # Add reviews
    reviews = course.reviews.all()[:10]
    data['reviews'] = CourseReviewSerializer(reviews, many=True).data
    return Response(data)


@api_view(['GET'])
@permission_classes([AllowAny])
def course_categories(request):
    """
    GET /api/catalog/courses/categories/
    List all unique categories.
    """
    # order_by() on the column itself: the model's default ordering would make DISTINCT per course
    categories = Course.objects.filter(status='published') \
        .order_by('category').values_list('category', flat=True).distinct()
    return Response(list(categories))


# ============================================
# ENROLLMENTS
# ============================================
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def my_enrollments(request):
    """
    GET  /api/catalog/enrollments/          → list my enrollments
    POST /api/catalog/enrollments/          → enroll in a course
    Body: { "course_id": 5 }
    """
    if request.method == 'GET':
        qs = Enrollment.objects.filter(member=request.user).select_related('course__trainer__user')
        return Response(EnrollmentSerializer(qs, many=True).data)

    # POST — enroll
    course_id = request.data.get('course_id')
    if not course_id:
        return Response({'detail': 'course_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    course = get_object_or_404(Course, id=course_id, status='published')

    if Enrollment.objects.filter(member=request.user, course=course).exists():
        return Response(
            {'detail': 'You are already enrolled in this course.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Paid courses are enrolled by the payment (apps.payments.gateway) once Moyasar confirms it.
    from apps.payments.gateway import course_is_free_for
    if not course_is_free_for(request.user, course):
        return Response(
            {'detail': 'This course is paid — complete the payment to enrol.', 'payment_required': True,
             'price': str(course.price)},
            status=status.HTTP_402_PAYMENT_REQUIRED,
        )

    enrollment = Enrollment.objects.create(member=request.user, course=course, amount_paid=0)
    Course.objects.filter(id=course.id).update(total_enrolled=F('total_enrolled') + 1)
    enrollment_started(enrollment)

    return Response(EnrollmentSerializer(enrollment).data, status=status.HTTP_201_CREATED)


@api_view(['POST', 'DELETE'])
@permission_classes([IsAuthenticated])
def complete_module(request, enrollment_id, module_id):
    """
    POST   /api/catalog/enrollments/<enrollment_id>/modules/<module_id>/complete/  → mark the day done
    DELETE (same URL)                                                            → undo (marked by mistake)
    A day that is locked until the previous one can only be completed in order.
    """
    enrollment = get_object_or_404(Enrollment, id=enrollment_id, member=request.user)
    module = get_object_or_404(CourseModule, id=module_id, course=enrollment.course)

    if request.method == 'DELETE':
        enrollment.completed_modules.remove(module)
    else:
        if enrollment.completed_modules.filter(id=module.id).exists():
            return Response({'detail': 'This day is already marked as done.'}, status=status.HTTP_400_BAD_REQUEST)
        if module.is_locked_until_prev:
            done = set(enrollment.completed_modules.values_list('id', flat=True))
            earlier = enrollment.course.modules.filter(order__lt=module.order).exclude(id__in=done).order_by('order').first()
            if earlier:
                return Response({'detail': f'Finish “{earlier.title}” first — days unlock in order.'},
                                status=status.HTTP_400_BAD_REQUEST)
        enrollment.completed_modules.add(module)

    total = enrollment.course.modules.count()
    completed = enrollment.completed_modules.count()
    enrollment.progress_percent = int((completed / total) * 100) if total > 0 else 0

    if enrollment.progress_percent == 100:
        enrollment.status = 'completed'
        enrollment.completed_at = timezone.now()
    elif enrollment.status == 'completed':
        enrollment.status = 'active'
        enrollment.completed_at = None

    enrollment.save()

    return Response(EnrollmentSerializer(enrollment).data)


# ============================================
# 1:1 SESSIONS
# ============================================
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def my_sessions(request):
    """
    GET  /api/catalog/sessions/         → my booked sessions
    POST /api/catalog/sessions/         → book a session
    Body: { "trainer_id": 3, "scheduled_at": "2025-11-14T09:30:00Z",
            "duration_minutes": 60, "focus_area": "Technique Analysis", "notes": "..." }
    """
    if request.method == 'GET':
        qs = TrainerSession.objects.filter(member=request.user).select_related('trainer__user')
        return Response(TrainerSessionSerializer(qs, many=True).data)

    # Book = reserve a free slot from the trainer's availability; it becomes
    # 'scheduled' once paid (apps.payments.gateway), or at once when the trainer's rate is 0.
    # The price is the trainer's hourly rate.
    from .slots import SLOT_MINUTES, MIN_LEAD_MINUTES, slot_is_free, within_availability

    serializer = TrainerSessionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    trainer = get_object_or_404(TrainerProfile, id=request.data.get('trainer_id'))
    if not (trainer.is_accepting_clients and trainer.offers_1on1):
        return Response({'detail': 'This trainer is not taking 1:1 bookings right now.'},
                        status=status.HTTP_400_BAD_REQUEST)

    start = serializer.validated_data['scheduled_at']
    if start < timezone.now() + timedelta(minutes=MIN_LEAD_MINUTES):
        return Response({'detail': 'Please pick a time at least an hour from now.'}, status=status.HTTP_400_BAD_REQUEST)
    if not within_availability(trainer, start, SLOT_MINUTES):
        return Response({'detail': 'The trainer is not available at that time.'}, status=status.HTTP_400_BAD_REQUEST)

    with db_transaction.atomic():
        TrainerProfile.objects.select_for_update().get(id=trainer.id)     # serialise bookings per trainer
        if not slot_is_free(trainer, start, SLOT_MINUTES):
            return Response({'detail': 'That slot was just taken — please pick another.'},
                            status=status.HTTP_409_CONFLICT)
        session = serializer.save(
            member=request.user, trainer=trainer, status='pending_payment',
            duration_minutes=SLOT_MINUTES, amount_charged=trainer.hourly_rate or 0,
        )
        if (session.amount_charged or 0) <= 0:          # a free session needs no payment step
            from apps.payments.gateway import confirm_session
            confirm_session(session)
    return Response(TrainerSessionSerializer(session).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_session(request, session_id):
    """
    POST /api/catalog/sessions/<id>/cancel/
    """
    # Unpaid bookings can always be cancelled. Paid ones up to REFUND_HOURS before the
    # start, refunded to locker credit (instant, no card refund round-trip).
    from apps.notifications.service import notify
    from apps.payments.models import LockerCredit

    with db_transaction.atomic():
        session = get_object_or_404(TrainerSession.objects.select_for_update(), id=session_id, member=request.user)
        if session.status == 'pending_payment':
            session.status = 'cancelled'
            session.save(update_fields=['status'])
            return Response(TrainerSessionSerializer(session).data)
        refused = {
            'completed': 'This session is already completed and cannot be cancelled.',
            'no_show': 'This session was marked as a no-show and cannot be cancelled.',
            'cancelled': 'This session is already cancelled.',
        }
        if session.status in refused:
            return Response({'detail': refused[session.status]}, status=status.HTTP_400_BAD_REQUEST)
        if session.status != 'scheduled':
            return Response({'detail': 'Only upcoming sessions can be cancelled.'}, status=status.HTTP_400_BAD_REQUEST)
        if session.scheduled_at <= timezone.now():
            return Response({'detail': 'This session has already started and cannot be cancelled.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if session.scheduled_at - timezone.now() < timedelta(hours=REFUND_HOURS):
            return Response(
                {'detail': f'Sessions can be cancelled up to {REFUND_HOURS} hours before the start.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        session.status = 'cancelled'
        session.save(update_fields=['status'])
        refund = session.amount_charged or 0
        if refund > 0:
            credit, _ = LockerCredit.objects.select_for_update().get_or_create(member=request.user)
            LockerCredit.objects.filter(id=credit.id).update(balance=F('balance') + refund)
            from apps.payments.gateway import paid_transaction, record_transaction
            record_transaction(request.user, f'Refund — cancelled session #{session.id}', 'refund', -refund,
                               'Locker credit', f'session:{session.id}',
                               refund_of=paid_transaction(trainer_session=session))
    if refund > 0:
        notify(request.user, 'trainer', 'Session cancelled',
               f'SAR {refund} was refunded to your locker credit.', icon='event_busy',
               action_label='View payments', action_path='/dashboard/payments')
    return Response(TrainerSessionSerializer(session).data)


REFUND_HOURS = 24


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def trainer_slots(request, trainer_id):
    """
    GET /api/catalog/trainers/<id>/slots/?date=YYYY-MM-DD
    Free 60-minute slots for that day, from the trainer's weekly availability.
    """
    from datetime import date as date_cls
    from .slots import available_slots
    trainer = get_object_or_404(TrainerProfile, id=trainer_id)
    try:
        day = date_cls.fromisoformat(request.query_params.get('date', ''))
    except ValueError:
        return Response({'detail': 'date must be YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)
    weekdays = sorted(set(trainer.availability.filter(is_active=True).values_list('day_of_week', flat=True)))
    return Response({
        'date': day.isoformat(),
        'price': str(trainer.hourly_rate),
        'duration_minutes': 60,
        'accepting': trainer.is_accepting_clients and trainer.offers_1on1,
        'available_weekdays': weekdays,          # 0 = Monday
        'slots': [{'start': s['start'].isoformat(), 'label': s['label']} for s in available_slots(trainer, day)],
    })


# ============================================
# REVIEWS
# ============================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def review_course(request, course_id):
    """
    POST /api/catalog/courses/<id>/review/
    Body: { "rating": 5, "comment": "Great course!" }
    """
    course = get_object_or_404(Course, id=course_id, status='published')

    if CourseReview.objects.filter(course=course, member=request.user).exists():
        return Response(
            {'detail': 'You already reviewed this course.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = CourseReviewSerializer(data=request.data)
    if serializer.is_valid():
        serializer.save(course=course, member=request.user)

        # Recalculate course rating
        reviews = course.reviews.all()
        course.rating = round(sum(r.rating for r in reviews) / reviews.count(), 2)
        course.total_reviews = reviews.count()
        course.save()

        return Response(serializer.data, status=status.HTTP_201_CREATED)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def review_trainer(request, trainer_id):
    """
    POST /api/catalog/trainers/<id>/review/
    Body: { "rating": 1-5, "comment": "..." } — sending again updates your review.
    """
    trainer = get_object_or_404(TrainerProfile, id=trainer_id)
    allowed, why = _can_review(request.user, trainer)
    if not allowed:
        return Response({'detail': why}, status=status.HTTP_403_FORBIDDEN)

    errors = {}
    try:
        rating = int(str(request.data.get('rating', '')).strip())
        if not 1 <= rating <= 5:
            raise ValueError
    except (TypeError, ValueError):
        errors['rating'] = 'Choose a rating from 1 to 5 stars.'
    comment = ' '.join(str(request.data.get('comment') or '').split())
    if len(comment) > 1000:
        errors['comment'] = 'Keep your review under 1000 characters.'
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)

    review, created = TrainerReview.objects.update_or_create(
        trainer=trainer, member=request.user, defaults={'rating': rating, 'comment': comment})
    _refresh_trainer_rating(trainer)
    return Response(TrainerReviewSerializer(review).data,
                    status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    # ============================================
# COURSE PLAYER
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def course_player(request, course_id):
    """
    GET /api/catalog/courses/<id>/player/
    Returns course details + member progress + current module.
    """
    course = get_object_or_404(
        Course.objects.select_related('trainer__user'),
        id=course_id,
        status='published',
    )

    # Check enrollment
    try:
        enrollment = Enrollment.objects.get(member=request.user, course=course)
    except Enrollment.DoesNotExist:
        return Response(
            {'detail': 'You are not enrolled in this course.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    completed_ids = set(enrollment.completed_modules.values_list('id', flat=True))
    modules = course.modules.all().order_by('order')

    module_data = []
    current_module = None
    for m in modules:
        is_completed = m.id in completed_ids
        is_current = False
        # Current = first uncompleted module
        if not is_completed and current_module is None:
            is_current = True
            current_module = m

        module_data.append({
            'id': m.id,
            'order': m.order,
            'title': m.title,
            'description': m.description,
            'week_number': m.week_number,
            'day_number': m.day_number,
            'video_url': m.video_url,
            'pdf_url': m.pdf_url,
            'duration_minutes': m.duration_minutes,
            'is_locked_until_prev': m.is_locked_until_prev,
            'is_completed': is_completed,
            'is_current': is_current,
        })

    total = len(module_data)
    done = sum(m['is_completed'] for m in module_data)
    return Response({
        'course': CourseListSerializer(course).data,
        'trainer': TrainerProfileSerializer(course.trainer).data,
        'enrollment': {
            'id': enrollment.id,
            'status': enrollment.status,
            'progress_percent': int(done * 100 / total) if total else 0,
            'completed_count': done,
            'total_modules': total,
        },
        'modules': module_data,
        'current_module': {
            'id': current_module.id,
            'title': current_module.title,
            'order': current_module.order,
        } if current_module else None,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def next_workout_suggestion(request):
    """
    GET /api/catalog/next-workout/
    Returns a suggested workout template for today based on active enrollments.
    """
    # Find active enrollment
    enrollment = (
        Enrollment.objects
        .filter(member=request.user, status='active')
        .select_related('course')
        .first()
    )

    if not enrollment:
        return Response({
            'has_suggestion': False,
            'message': 'Enroll in a course to unlock guided workouts.',
        })

    course = enrollment.course
    # Return the current module details
    completed_ids = set(enrollment.completed_modules.values_list('id', flat=True))
    next_module = course.modules.exclude(id__in=completed_ids).order_by('order').first()

    if not next_module:
        return Response({
            'has_suggestion': False,
            'message': 'You have completed all modules in this course.',
        })

    return Response({
        'has_suggestion': True,
        'course_id': course.id,
        'course_title': course.title, 'course_title_ar': course.title_ar,
        'module': {
            'id': next_module.id,
            'title': next_module.title,
            'order': next_module.order,
            'description': next_module.description,
            'duration_minutes': next_module.duration_minutes,
        },
    })