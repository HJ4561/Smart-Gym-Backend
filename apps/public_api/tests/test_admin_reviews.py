"""Admin → Reviews: list course and coach reviews, filter them, delete one (rating updates at once)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.approvals.models import ChangeRequest
from apps.approvals.registry import submit_create
from apps.catalog.models import Course, CourseReview, TrainerProfile, TrainerReview

User = get_user_model()


class AdminReviewTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        self.member = User.objects.create_user(username='me', email='me@t.local', password='x', first_name='Sara')
        cu = User.objects.create_user(username='co', email='co@t.local', password='x', role='trainer', first_name='Omar')
        self.coach = TrainerProfile.objects.create(user=cu, title='Coach', hourly_rate=Decimal('100'))
        self.course = Course.objects.create(title='Strength 101', description='x', category='strength',
                                            trainer=self.coach, status='published')
        self.cr = CourseReview.objects.create(course=self.course, member=self.member, rating=1, comment='Rude spam',
                                              status='approved')
        self.tr = TrainerReview.objects.create(trainer=self.coach, member=self.member, rating=5, comment='Great coach',
                                               status='pending')
        submit_create(self.member, 'catalog.trainer_review', self.tr)
        self.client.force_authenticate(self.admin)

    def test_list_and_filter(self):
        r = self.client.get('/api/admin/reviews/')
        self.assertEqual(r.data['count'], 2)
        self.assertEqual([x['type'] for x in self.client.get('/api/admin/reviews/?type=course').data['results']], ['course'])
        self.assertEqual(self.client.get('/api/admin/reviews/?rating=5').data['results'][0]['comment'], 'Great coach')
        self.assertEqual(self.client.get('/api/admin/reviews/?q=spam').data['count'], 1)
        self.assertEqual(self.client.get('/api/admin/reviews/?status=pending').data['count'], 1)

    def test_delete_updates_rating_and_withdraws_from_the_queue(self):
        from apps.catalog.reviews import refresh_course_rating
        refresh_course_rating(self.course)
        self.course.refresh_from_db()
        self.assertEqual(self.course.total_reviews, 1)
        self.assertEqual(self.client.delete(f'/api/admin/reviews/course/{self.cr.id}/').status_code, 204)
        self.course.refresh_from_db()
        self.assertEqual(self.course.total_reviews, 0)
        self.assertEqual(self.client.delete(f'/api/admin/reviews/trainer/{self.tr.id}/').status_code, 204)
        self.assertEqual(ChangeRequest.objects.get(target_type='catalog.trainer_review').status, 'withdrawn')

    def test_only_admins(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get('/api/admin/reviews/').status_code, 403)
        self.assertEqual(self.client.delete(f'/api/admin/reviews/course/{self.cr.id}/').status_code, 403)
