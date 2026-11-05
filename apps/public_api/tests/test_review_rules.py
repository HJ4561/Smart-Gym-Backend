"""Reviews only from members who paid for (or booked and had) the course or the coach: 400 otherwise."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.catalog.models import Course, Enrollment, TrainerSession

from .test_rules_round2 import Base


class ReviewRuleTests(Base):
    def setUp(self):
        super().setUp()
        self.course = Course.objects.create(trainer=self.coach, title='Strength 101', category='Strength',
                                            price=Decimal('200'), status='published')
        self.client.force_authenticate(self.member)

    def course_review(self):
        return self.client.post(f'/api/catalog/courses/{self.course.id}/review/', {'rating': 5, 'comment': 'Great'},
                                format='json')

    def trainer_review(self):
        return self.client.post(f'/api/catalog/trainers/{self.coach.id}/review/', {'rating': 4}, format='json')

    def test_course_needs_an_enrollment(self):
        r = self.course_review()
        self.assertEqual(r.status_code, 400)
        self.assertIn('after you join it', r.data['detail'])
        Enrollment.objects.create(member=self.member, course=self.course, amount_paid=Decimal('200'))
        self.assertEqual(self.course_review().status_code, 201)
        self.course.refresh_from_db()
        self.assertEqual(self.course.total_reviews, 1)

    def test_refunded_enrollment_cannot_review(self):
        Enrollment.objects.create(member=self.member, course=self.course, status='refunded')
        self.assertEqual(self.course_review().status_code, 400)
        self.assertEqual(self.trainer_review().status_code, 400)     # nor the coach through that course

    def test_coach_needs_a_session_that_happened_or_a_course(self):
        TrainerSession.objects.create(member=self.member, trainer=self.coach, status='scheduled',
                                      scheduled_at=timezone.now() + timedelta(days=2))
        self.assertEqual(self.trainer_review().status_code, 400)     # booked, but not had yet
        TrainerSession.objects.create(member=self.member, trainer=self.coach, status='cancelled',
                                      scheduled_at=timezone.now() - timedelta(days=2))
        self.assertEqual(self.trainer_review().status_code, 400)     # cancelled = refunded
        TrainerSession.objects.create(member=self.member, trainer=self.coach, status='no_show',
                                      scheduled_at=timezone.now() - timedelta(days=1))
        self.assertEqual(self.trainer_review().status_code, 201)     # paid for and missed it: may review

    def test_course_of_the_coach_allows_reviewing_the_coach(self):
        Enrollment.objects.create(member=self.member, course=self.course, status='completed')
        self.assertEqual(self.trainer_review().status_code, 201)

    def test_public_website_reviews_come_from_the_database(self):
        from django.core.cache import cache
        Course.objects.filter(pk=self.course.pk).update(title_ar='القوة 101')
        Enrollment.objects.create(member=self.member, course=self.course, status='active')
        self.course_review()
        cache.delete('public-live-v1')
        live = self.client.get('/api/public/live/').data
        self.assertEqual(live['reviews_count'], 1)
        self.assertEqual({k: live['reviews'][0][k] for k in ('name', 'comment', 'about', 'about_ar', 'kind')},
                         {'name': 'Sara A.', 'comment': 'Great', 'about': 'Strength 101', 'about_ar': 'القوة 101',
                          'kind': 'course'})
