"""Approval queue, member reviews waiting for an admin, receptionist modules and the staff activity log."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.catalog.models import Course, Enrollment, TrainerProfile, TrainerReview, TrainerSession
from apps.staff.models import StaffProfile

from .models import ChangeRequest, StaffActivity

User = get_user_model()
PW = 'Str0ng-Pass-2026!'


class Base(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password=PW, role='admin', first_name='Ad')
        self.member = User.objects.create_user(username='m', email='m@t.local', password=PW, role='member',
                                               first_name='Sara', last_name='Ali')
        coach_user = User.objects.create_user(username='c', email='c@t.local', password=PW, role='trainer',
                                              first_name='Omar')
        self.coach = TrainerProfile.objects.create(user=coach_user, title='Coach', hourly_rate=Decimal('100'))
        self.course = Course.objects.create(title='Strength 101', description='x', category='Strength',
                                            trainer=self.coach, status='published', price=0)
        Enrollment.objects.create(member=self.member, course=self.course, status='active')
        TrainerSession.objects.create(member=self.member, trainer=self.coach, status='completed',
                                      scheduled_at=timezone.now() - timedelta(days=1), duration_minutes=60)

    def as_(self, user):
        self.client.force_authenticate(user)


class ReviewApprovalTests(Base):
    def test_course_review_waits_then_counts_once_approved(self):
        self.as_(self.member)
        r = self.client.post(f'/api/catalog/courses/{self.course.id}/review/', {'rating': 4, 'comment': 'Solid plan'},
                             format='json')
        self.assertEqual((r.status_code, r.data['status']), (201, 'pending'))
        self.assertEqual(self.client.get(f'/api/catalog/courses/{self.course.id}/').data['reviews'], [])
        self.course.refresh_from_db()
        self.assertEqual(self.course.total_reviews, 0)

        self.as_(self.admin)
        queue = self.client.get('/api/admin/approvals/').data
        self.assertEqual(queue['count'], 1)
        cr = queue['results'][0]
        self.assertEqual((cr['type'], cr['kind']), ('catalog.course_review', 'create'))
        r = self.client.post(f"/api/admin/approvals/{cr['id']}/approve/", {}, format='json')
        self.assertEqual((r.status_code, r.data['status']), (200, 'approved'))

        self.as_(self.member)
        self.assertEqual(len(self.client.get(f'/api/catalog/courses/{self.course.id}/').data['reviews']), 1)
        self.course.refresh_from_db()
        self.assertEqual((self.course.total_reviews, float(self.course.rating)), (1, 4.0))

    def test_coach_review_can_be_edited_by_the_admin_or_rejected(self):
        self.as_(self.member)
        self.client.post(f'/api/catalog/trainers/{self.coach.id}/review/', {'rating': 5, 'comment': 'Great!!!'},
                         format='json')
        self.as_(self.admin)
        cr = ChangeRequest.objects.get(target_type='catalog.trainer_review')
        r = self.client.post(f'/api/admin/approvals/{cr.id}/approve/', {'payload': {'comment': 'Great coach.'}},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        review = TrainerReview.objects.get()
        self.assertEqual((review.status, review.comment), ('approved', 'Great coach.'))
        self.coach.refresh_from_db()
        self.assertEqual(self.coach.total_reviews, 1)

        # changing the review sends it back to the queue and takes it off the coach's rating meanwhile
        self.as_(self.member)
        self.client.post(f'/api/catalog/trainers/{self.coach.id}/review/', {'rating': 1, 'comment': 'Bad'}, format='json')
        self.coach.refresh_from_db()
        self.assertEqual(self.coach.total_reviews, 0)
        self.as_(self.admin)
        cr = ChangeRequest.objects.get(target_type='catalog.trainer_review', status='pending')
        self.assertEqual(self.client.post(f'/api/admin/approvals/{cr.id}/reject/', {}, format='json').status_code, 400)
        r = self.client.post(f'/api/admin/approvals/{cr.id}/reject/', {'note': 'Abusive'}, format='json')
        self.assertEqual(r.data['status'], 'rejected')
        self.assertEqual(TrainerReview.objects.get().status, 'rejected')
        self.assertTrue(self.member.notifications.filter(title__startswith='Not approved').exists())

    def test_only_admins_see_the_queue(self):
        self.as_(self.member)
        self.assertEqual(self.client.get('/api/admin/approvals/').status_code, 403)
        self.as_(self.coach.user)
        self.assertEqual(self.client.get('/api/admin/approvals/').status_code, 403)
        self.assertEqual(self.client.get('/api/approvals/mine/').status_code, 200)


class DeskModuleTests(Base):
    def test_admin_ticks_modules_and_the_receptionist_sees_them(self):
        self.as_(self.admin)
        r = self.client.post('/api/admin/staff/', {'email': 'desk@t.local', 'first_name': 'Reem', 'role': 'receptionist',
                                                   'modules': ['gate', 'registrations'], 'password': PW},
                             format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['modules'], ['registrations', 'gate'])
        self.assertTrue(r.data['can_sign_in'])
        bad = self.client.patch(f"/api/admin/staff/{r.data['id']}/", {'modules': ['everything']}, format='json')
        self.assertIn('modules', bad.data)
        desk = User.objects.get(email='desk@t.local')
        self.as_(desk)
        self.assertEqual(self.client.get('/api/auth/me/').data['modules'], ['registrations', 'gate'])
        StaffProfile.objects.filter(user=desk).update(status='suspended')
        self.as_(User.objects.get(email='desk@t.local'))
        self.assertEqual(self.client.get('/api/auth/me/').data['modules'], [])

    def test_shop_manager_and_nutritionist_get_their_own_portal(self):
        self.as_(self.admin)
        for email, role in (('shop@t.local', 'shop_manager'), ('food@t.local', 'nutritionist')):
            r = self.client.post('/api/admin/staff/', {'email': email, 'first_name': 'Lina', 'last_name': role[:4],
                                                       'role': role, 'password': PW}, format='json')
            self.assertEqual((r.status_code, r.data['can_sign_in']), (201, True), r.data)
        self.assertEqual(self.client.get('/api/admin/staff/?role=shop_manager').data[0]['department'], 'retail')
        self.assertTrue(User.objects.get(email='food@t.local').nutritionist_profile)


class ActivityLogTests(Base):
    def test_staff_changes_are_logged_and_members_are_not(self):
        self.as_(self.admin)
        self.client.post('/api/admin/staff/', {'email': 'desk@t.local', 'first_name': 'Reem', 'role': 'receptionist'},
                         format='json')
        self.as_(self.member)
        self.client.post(f'/api/catalog/courses/{self.course.id}/review/', {'rating': 5}, format='json')
        self.assertEqual(StaffActivity.objects.count(), 1)
        self.as_(self.admin)
        log = self.client.get('/api/admin/staff-activity/').data
        row = log['results'][0]
        self.assertEqual((row['action'], row['area'], row['actor']['email']), ('Added staff', 'team', 'a@t.local'))
        self.assertEqual(self.client.get('/api/admin/staff-activity/?area=shop').data['count'], 0)
        self.assertEqual(log['people'][0]['actions'], 1)
