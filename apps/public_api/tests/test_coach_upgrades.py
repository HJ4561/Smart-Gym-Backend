"""Coach portal upgrades: student details, class dates and posts, video approval, moving 1-on-1s, live course edits."""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.approvals.models import ChangeRequest
from apps.approvals.registry import approve
from apps.catalog.models import Course, CourseModule, Enrollment, TrainerProfile, TrainerSession
from apps.classes.models import ClassEnrollment, GymClass
from apps.notifications.models import Notification
from apps.workouts.models import Exercise, ExerciseVideo

User = get_user_model()


class CoachUpgradeTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        cu = User.objects.create_user(username='co', email='co@t.local', password='x', role='trainer', first_name='Omar')
        self.coach = TrainerProfile.objects.create(user=cu, title='Coach', hourly_rate=Decimal('100'))
        self.member = User.objects.create_user(username='me', email='me@t.local', password='x', first_name='Sara')
        self.stranger = User.objects.create_user(username='st', email='st@t.local', password='x')
        self.course = Course.objects.create(title='Strength 101', description='Lift', category='strength',
                                            trainer=self.coach, status='published', price=Decimal('200'))
        CourseModule.objects.create(course=self.course, order=1, title='Day 1')
        Enrollment.objects.create(member=self.member, course=self.course, amount_paid=Decimal('200'))
        self.cls = GymClass.objects.create(name='Morning HIIT', category='hiit', trainer=self.coach,
                                           day_of_week=timezone.localdate().weekday(), start_time=time(7, 0))
        ClassEnrollment.objects.create(gym_class=self.cls, member=self.member, status='enrolled')
        self.client.force_authenticate(cu)

    def test_student_details_only_for_own_students(self):
        r = self.client.get(f'/api/coach/students/{self.member.id}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['courses'][0]['total'], 1)
        self.assertIn('visits_30d', r.data['insights'])
        self.assertEqual(self.client.get(f'/api/coach/students/{self.stranger.id}/').status_code, 404)

    def test_classes_have_upcoming_and_past_dates(self):
        up = self.client.get('/api/coach/classes/').data
        self.assertEqual(len(up[0]['dates']), 4)
        self.assertEqual(up[0]['dates'][0]['date'], timezone.localdate())

    def test_class_message_reaches_members_and_video_waits(self):
        r = self.client.post(f'/api/coach/classes/{self.cls.id}/posts/',
                             {'kind': 'message', 'title': 'Bring water', 'body': 'Hot day tomorrow.'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(Notification.objects.filter(recipient=self.member, title__contains='Bring water').exists())
        r = self.client.post(f'/api/coach/classes/{self.cls.id}/posts/',
                             {'kind': 'video', 'title': 'Warm-up drill', 'video_url': 'https://youtu.be/dQw4w9WgXcQ'},
                             format='json')
        self.assertEqual(r.data['approval_status'], 'pending')
        self.assertTrue(r.data['thumbnail_url'].startswith('https://img.youtube.com/'))
        self.client.force_authenticate(self.member)
        titles = [p['title'] for p in self.client.get(f'/api/classes/{self.cls.id}/posts/').data]
        self.assertEqual(titles, ['Bring water'])
        approve(ChangeRequest.objects.get(target_type='coach.class_video'), self.admin)
        titles = [p['title'] for p in self.client.get(f'/api/classes/{self.cls.id}/posts/').data]
        self.assertIn('Warm-up drill', titles)
        self.client.force_authenticate(self.stranger)
        self.assertEqual(self.client.get(f'/api/classes/{self.cls.id}/posts/').status_code, 403)

    def test_new_video_waits_for_approval(self):
        ex = Exercise.objects.create(name='Leg Press', machine_id='LP-1')
        r = self.client.post('/api/coach/videos/', {'exercise_id': ex.id, 'title': 'Leg press basics',
                                                    'video_url': 'https://youtu.be/dQw4w9WgXcQ', 'tier': 'basic'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.client.force_authenticate(self.member)
        guide = self.client.get(f'/api/workouts/exercises/{ex.id}/videos/')
        if guide.status_code == 200:
            self.assertNotIn('Leg press basics', [v['title'] for v in guide.data.get('videos', [])])
        v = ExerciseVideo.objects.get(title='Leg press basics')
        approve(ChangeRequest.objects.get(target_type='coach.video', object_id=v.id), self.admin)
        v.refresh_from_db()
        self.assertEqual(v.approval_status, 'approved')

    def test_move_and_delay_a_session(self):
        start = timezone.now() + timedelta(days=2)
        s = TrainerSession.objects.create(member=self.member, trainer=self.coach, scheduled_at=start, status='scheduled')
        r = self.client.post(f'/api/coach/sessions/{s.id}/change/', {'delay_minutes': 30, 'reason': 'Traffic'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        s.refresh_from_db()
        self.assertEqual(s.scheduled_at, start + timedelta(minutes=30))
        self.assertEqual((s.rescheduled_from, s.change_note), (start, 'Traffic'))
        self.assertTrue(Notification.objects.filter(recipient=self.member, title__contains='Session moved').exists())
        past = (timezone.now() - timedelta(hours=3)).isoformat()
        self.assertEqual(self.client.post(f'/api/coach/sessions/{s.id}/change/', {'scheduled_at': past},
                                          format='json').status_code, 400)

    def test_live_course_change_waits_and_admin_can_edit_the_price(self):
        r = self.client.patch(f'/api/coach/courses/{self.course.id}/', {'price': '250', 'description': 'Lift more'},
                              format='json')
        self.assertEqual(r.status_code, 202, r.data)
        self.course.refresh_from_db()
        self.assertEqual((self.course.price, self.course.description), (Decimal('200.00'), 'Lift'))
        approve(ChangeRequest.objects.get(target_type='coach.course'), self.admin,
                payload={'price': 220, 'description': 'Lift more'})
        self.course.refresh_from_db()
        self.assertEqual((self.course.price, self.course.description), (Decimal('220.00'), 'Lift more'))

    def test_live_course_lessons_change_on_approval(self):
        lessons = [{'title': 'Day 1'}, {'title': 'Day 2', 'week_number': 1, 'day_number': 2}]
        r = self.client.patch(f'/api/coach/courses/{self.course.id}/', {'modules': lessons}, format='json')
        self.assertEqual(r.status_code, 202, r.data)
        self.assertEqual(self.course.modules.count(), 1)
        approve(ChangeRequest.objects.get(target_type='coach.course'), self.admin)
        self.assertEqual(self.course.modules.count(), 2)


class CoachProfileApprovalTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        cu = User.objects.create_user(username='co', email='co@t.local', password='x', role='trainer', first_name='Omar')
        self.coach = TrainerProfile.objects.create(user=cu, title='Coach', hourly_rate=Decimal('100'))
        self.client.force_authenticate(cu)

    def test_name_and_title_wait_and_admin_can_edit_before_approving(self):
        r = self.client.patch('/api/coach/me/', {'first_name': 'Omari', 'title': 'Head coach', 'phone': '0551234567'},
                              format='json')
        self.assertEqual(r.status_code, 202, r.data)
        self.coach.refresh_from_db()
        self.coach.user.refresh_from_db()
        self.assertEqual((self.coach.user.first_name, self.coach.title), ('Omar', 'Coach'))
        self.assertTrue(self.coach.user.phone)                        # private settings change at once
        approve(ChangeRequest.objects.get(target_type='coach.profile'), self.admin,
                payload={'first_name': 'Omari', 'title': 'Senior coach'})
        self.coach.refresh_from_db()
        self.coach.user.refresh_from_db()
        self.assertEqual((self.coach.user.first_name, self.coach.title), ('Omari', 'Senior coach'))


class NewCourseQueueTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        cu = User.objects.create_user(username='co', email='co@t.local', password='x', role='trainer', first_name='Omar')
        self.coach = TrainerProfile.objects.create(user=cu, title='Coach', hourly_rate=Decimal('100'))
        self.course = Course.objects.create(title='Kettlebell basics', description='Swing safely', category='strength',
                                            trainer=self.coach, status='draft', price=Decimal('150'))
        CourseModule.objects.create(course=self.course, order=1, title='Day 1')
        self.client.force_authenticate(cu)

    def submit(self):
        return self.client.post(f'/api/coach/courses/{self.course.id}/submit/')

    def test_submitted_course_is_queued_and_approval_publishes_it(self):
        self.assertEqual(self.submit().status_code, 200)
        cr = ChangeRequest.objects.get(target_type='coach.course_new')
        approve(cr, self.admin, payload={'price': 120})              # the admin sets the price before publishing
        self.course.refresh_from_db()
        self.assertEqual((self.course.status, self.course.price), ('published', Decimal('120.00')))

    def test_rejection_sends_it_back_with_the_note(self):
        from apps.approvals.registry import reject
        self.submit()
        reject(ChangeRequest.objects.get(target_type='coach.course_new'), self.admin, note='Add a warm-up day')
        self.course.refresh_from_db()
        self.assertEqual((self.course.status, self.course.review_note), ('draft', 'Add a warm-up day'))
        self.assertEqual(self.submit().status_code, 200)              # fixed and sent again
        self.assertEqual(ChangeRequest.objects.filter(target_type='coach.course_new', status='pending').count(), 1)

    def test_publishing_from_the_coaches_page_closes_the_queue_entry(self):
        self.submit()
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f'/api/admin/courses/{self.course.id}/', {'status': 'published'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(ChangeRequest.objects.get(target_type='coach.course_new').status, 'approved')
        self.assertEqual(self.client.get('/api/admin/approvals/').data['courses_waiting'], 0)
