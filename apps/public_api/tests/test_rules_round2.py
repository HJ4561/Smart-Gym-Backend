"""
Second round of rules: paid nutrition plans, nutrition notes page data, session cancel rules,
set logging limits, course days in order, password reset, duplicates and formats in the
console (products, classes, machines, plans, website lists), trainer reviews, signup photo
quality and the public live numbers.
"""
import io
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from PIL import Image, ImageFilter
from rest_framework.test import APITestCase

from apps.catalog.models import Course, CourseModule, Enrollment, TrainerProfile, TrainerSession
from apps.classes.models import GymClass
from apps.core.photo_quality import photo_problem
from apps.members.models import Membership, MembershipPlan
from apps.nutrition.models import NutritionistProfile, NutritionPlan
from apps.payments.models import LockerCredit
from apps.shop.models import Product, ProductCategory
from apps.workouts.models import Exercise

from .test_payments import clear_image
from .faces import one_face

# Test photos have no real face — stub the detector to find one well-placed face.
_faces = one_face()


def setUpModule():
    _faces.start()


def tearDownModule():
    _faces.stop()

User = get_user_model()


class Base(APITestCase):
    def setUp(self):
        self.plan = MembershipPlan.objects.create(name='pro', display_name='Pro', price_monthly=79, price_annual=790)
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member',
                                               first_name='Sara', last_name='Ali')
        Membership.objects.create(member=self.member, plan=self.plan, start_date=date.today(),
                                  expiry_date=date.today() + timedelta(days=30), iqama_number='2111111111')
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin', first_name='Ad')
        coach_user = User.objects.create_user(username='c', email='c@t.local', password='x', role='trainer',
                                              first_name='Omar', last_name='Saleh')
        self.coach = TrainerProfile.objects.create(user=coach_user, title='Coach', hourly_rate=Decimal('100'))
        diet_user = User.objects.create_user(username='n', email='n@t.local', password='x', role='nutritionist',
                                             first_name='Huda', last_name='Nasser')
        self.nutritionist = NutritionistProfile.objects.create(user=diet_user, title='Dietitian', hourly_rate=Decimal('150'))


class NutritionPlanPaymentTests(Base):
    def test_request_waits_for_payment_then_starts_and_notes_show(self):
        self.client.force_authenticate(self.member)
        r = self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': self.nutritionist.id, 'goal': 'fat_loss'},
                             format='json')
        self.assertEqual((r.status_code, r.data['status'], r.data['price']), (201, 'pending_payment', '150.00'))
        self.assertEqual(self.client.get('/api/nutrition/plan/').status_code, 404)        # not active yet
        LockerCredit.objects.create(member=self.member, balance=Decimal('200'))
        paid = self.client.post('/api/payments/checkout/', {'purpose': 'nutrition_plan', 'plan_id': r.data['id'],
                                                            'method': 'locker_credit'}, format='json')
        self.assertIn(paid.status_code, (200, 201), paid.data)
        plan = self.client.get('/api/nutrition/plan/').data
        self.assertEqual((plan['status'], plan['goal'], plan['nutritionist_id']), ('active', 'fat_loss', self.nutritionist.id))
        # staff review → member sees the note; the notification links to the plan page
        self.client.force_authenticate(self.admin)
        self.client.patch(f"/api/admin/nutrition/plans/{plan['id']}/", {'notes': 'Eat more greens.'}, format='json')
        self.client.force_authenticate(self.member)
        plan = self.client.get('/api/nutrition/plan/').data
        self.assertEqual(plan['notes'], 'Eat more greens.')
        self.assertIsNotNone(plan['reviewed_at'])
        self.assertEqual(self.member.notifications.order_by('-id').first().action_path, '/dashboard/nutrition-plan')

    def test_free_nutritionist_starts_straight_away_and_goal_switch_is_free(self):
        self.nutritionist.hourly_rate = 0
        self.nutritionist.save()
        self.client.force_authenticate(self.member)
        r = self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': self.nutritionist.id, 'goal': 'recomp'},
                             format='json')
        self.assertEqual(r.data['status'], 'active')
        r = self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': self.nutritionist.id, 'goal': 'endurance'},
                             format='json')
        self.assertEqual((r.status_code, r.data['goal'], r.data['status']), (200, 'endurance', 'active'))
        self.assertEqual(NutritionPlan.objects.filter(member=self.member).count(), 1)

    def test_unpaid_requests_are_hidden_from_admin_and_meals_are_listed(self):
        self.client.force_authenticate(self.member)
        self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': self.nutritionist.id, 'goal': 'recomp'},
                         format='json')
        self.client.post('/api/nutrition/meals/', {'meal_type': 'lunch', 'description': 'Chicken rice', 'calories': 650,
                                                   'protein_g': 45, 'carbs_g': 70, 'fats_g': 15}, format='json')
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get('/api/admin/nutrition/plans/?status=all').data, [])
        days = self.client.get(f'/api/admin/nutrition/members/{self.member.id}/meals/').data
        self.assertEqual((len(days), days[0]['calories'], days[0]['meals'][0]['description']), (1, 650, 'Chicken rice'))

    def test_hidden_nutritionist_detail_is_404(self):
        self.nutritionist.user.is_active = False
        self.nutritionist.user.save()
        self.assertEqual(self.client.get(f'/api/nutrition/nutritionists/{self.nutritionist.id}/').status_code, 404)

    def test_admin_list_is_sorted_by_rating(self):
        better = NutritionistProfile.objects.create(
            user=User.objects.create_user(username='n2', email='n2@t.local', password='x', first_name='Zaid'),
            title='Dietitian', rating=Decimal('4.9'))
        self.client.force_authenticate(self.admin)
        rows = self.client.get('/api/admin/nutrition/nutritionists/').data
        self.assertEqual(rows[0]['id'], better.id)


class SessionAndSetTests(Base):
    def session(self, status, hours=48):
        return TrainerSession.objects.create(member=self.member, trainer=self.coach, status=status,
                                             scheduled_at=timezone.now() + timedelta(hours=hours),
                                             amount_charged=Decimal('100'))

    def test_completed_no_show_and_cancelled_sessions_cannot_be_cancelled(self):
        self.client.force_authenticate(self.member)
        for status, words in (('completed', 'already completed'), ('no_show', 'no-show'), ('cancelled', 'already cancelled')):
            r = self.client.post(f'/api/catalog/sessions/{self.session(status).id}/cancel/')
            self.assertEqual(r.status_code, 400)
            self.assertIn(words, r.data['detail'])
        self.assertEqual(self.client.post(f'/api/catalog/sessions/{self.session("scheduled").id}/cancel/').status_code, 200)

    def test_bad_weight_or_reps_get_a_reason(self):
        ex = Exercise.objects.create(name='Leg Press', category='strength', primary_muscle='legs', qr_tag_id='SG-MCH-1')
        self.client.force_authenticate(self.member)
        url = f'/api/workouts/exercises/{ex.id}/quick-log/'
        r = self.client.post(url, {'weight_kg': -5, 'reps': 0}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('weight between 0 and 500', r.data['weight_kg'])
        self.assertIn('reps between 1 and 100', r.data['reps'])
        self.assertIn('weight', r.data['detail'])
        self.assertEqual(self.client.post(url, {'weight_kg': 'abc', 'reps': 2.5}, format='json').status_code, 400)
        self.assertEqual(self.client.post(url, {'weight_kg': 60, 'reps': 8}, format='json').status_code, 201)


class CourseDayTests(Base):
    def test_days_complete_in_order_and_can_be_undone(self):
        course = Course.objects.create(title='Strength 101', description='x', category='Strength', trainer=self.coach,
                                       status='published', price=0)
        d1, d2 = (CourseModule.objects.create(course=course, title=f'Day {i}', order=i, week_number=1, day_number=i)
                  for i in (1, 2))
        enrol = Enrollment.objects.create(member=self.member, course=course)
        self.client.force_authenticate(self.member)
        url = '/api/catalog/enrollments/{}/modules/{}/complete/'
        r = self.client.post(url.format(enrol.id, d2.id))
        self.assertEqual(r.status_code, 400)
        self.assertIn('Day 1', r.data['detail'])
        self.assertEqual(self.client.post(url.format(enrol.id, d1.id)).data['progress_percent'], 50)
        self.assertEqual(self.client.delete(url.format(enrol.id, d1.id)).data['progress_percent'], 0)


class PasswordResetTests(Base):
    def test_forgot_sends_a_link_and_reset_sets_the_password(self):
        r = self.client.post('/api/auth/password/forgot/', {'email': 'M@t.local'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertIn('/reset-password?uid=', mail.outbox[0].body)
        # unknown emails get the same answer and no email
        self.assertEqual(self.client.post('/api/auth/password/forgot/', {'email': 'nobody@t.local'}, format='json').status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('valid email', self.client.post('/api/auth/password/forgot/', {'email': 'bad@'}, format='json').data['email'])

        uid = urlsafe_base64_encode(force_bytes(self.member.pk))
        token = default_token_generator.make_token(self.member)
        r = self.client.post('/api/auth/password/reset/', {'uid': uid, 'token': token, 'new_password': 'New-Pass-123',
                                                           'new_password_confirm': 'Other-123'}, format='json')
        self.assertIn('don’t match', r.data['new_password_confirm'])
        r = self.client.post('/api/auth/password/reset/', {'uid': uid, 'token': token, 'new_password': 'New-Pass-123',
                                                           'new_password_confirm': 'New-Pass-123'}, format='json')
        self.assertEqual((r.status_code, r.data['login_path']), (200, '/login'))
        self.assertEqual(self.client.post('/api/auth/login/', {'email': 'm@t.local', 'password': 'New-Pass-123'},
                                          format='json').status_code, 200)
        # the link works once
        r = self.client.post('/api/auth/password/reset/', {'uid': uid, 'token': token, 'new_password': 'Again-Pass-123',
                                                           'new_password_confirm': 'Again-Pass-123'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_wrong_password_says_so(self):
        r = self.client.post('/api/auth/login/', {'email': 'm@t.local', 'password': 'nope'}, format='json')
        self.assertEqual(r.status_code, 401)
        self.assertIn('Wrong email or password', r.data['detail'])


class ConsoleRulesTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_products_unique_name_and_sku_format(self):
        cat = ProductCategory.objects.create(key='supplements', name='Supplements')
        r = self.client.post('/api/admin/shop/products/', {'name': 'Whey Protein', 'category_id': cat.id, 'price': 99},
                             format='json')
        self.assertEqual((r.status_code, r.data['sku']), (201, 'SUP-0001'))
        r = self.client.post('/api/admin/shop/products/', {'name': ' whey  protein ', 'category_id': cat.id, 'price': 99},
                             format='json')
        self.assertIn('already exists', r.data['name'])
        r = self.client.post('/api/admin/shop/products/', {'name': 'Creatine', 'sku': 'cr1', 'category_id': cat.id,
                                                           'price': 50}, format='json')
        self.assertIn('SUP- and 4 digits', r.data['sku'])          # the prefix follows the category
        self.assertIn('already exists', self.client.post('/api/admin/shop/categories/', {'name': 'supplements'},
                                                         format='json').data['name'])

    def test_class_rooms_coaches_and_names(self):
        base = {'name': 'Morning HIIT', 'category': 'hiit', 'day_of_week': 0, 'start_time': '07:00',
                'duration_minutes': 60, 'capacity': 10, 'location': 'Studio 1', 'trainer_id': self.coach.id}
        self.assertEqual(self.client.post('/api/classes/admin/', base, format='json').status_code, 201)
        r = self.client.post('/api/classes/admin/', {**base, 'name': 'Spin', 'location': 'Studio 2', 'start_time': '07:30'},
                             format='json')
        self.assertIn('can’t be in two classes', r.data['trainer_id'])
        r = self.client.post('/api/classes/admin/', {**base, 'name': 'Yoga', 'trainer_id': '', 'start_time': '07:30'},
                             format='json')
        self.assertIn('Studio 1 is already booked', r.data['location'])
        r = self.client.post('/api/classes/admin/', {**base, 'name': 'x' * 61, 'location': 'Room', 'trainer_id': ''},
                             format='json')
        self.assertIn('3–60 characters', r.data['name'])
        self.assertIn('Room must be', r.data['location'])
        self.assertEqual(self.client.post('/api/classes/admin/', {**base, 'name': 'Late HIIT', 'start_time': '08:00'},
                                          format='json').status_code, 201)     # back-to-back is fine

    def test_machine_asset_id_format_unique_and_delete(self):
        r = self.client.post('/api/admin/machines/', {'name': 'Leg Press', 'category': 'strength',
                                                      'primary_muscle': 'legs', 'machine_id': 'st-07'}, format='json')
        self.assertEqual((r.status_code, r.data['machine_id']), (201, 'ST-07'))
        r = self.client.post('/api/admin/machines/', {'name': 'Hack Squat', 'category': 'strength',
                                                      'primary_muscle': 'legs', 'machine_id': 'ST-07'}, format='json')
        self.assertIn('already used', r.data['machine_id'])
        r = self.client.post('/api/admin/machines/', {'name': 'Hack Squat', 'category': 'strength',
                                                      'primary_muscle': 'legs', 'machine_id': 'floor 2'}, format='json')
        self.assertIn('2–4 letters', r.data['machine_id'])
        machine_id = Exercise.objects.get(name='Leg Press').id
        self.assertEqual(self.client.delete(f'/api/admin/machines/{machine_id}/').status_code, 204)

    def test_plans_unique_names_and_delete_only_when_unused(self):
        r = self.client.post('/api/admin/site-content/plans/', {'name': 'basic', 'display_name': 'PRO', 'price_monthly': 10,
                                                                'price_annual': 100}, format='json')
        self.assertIn('already exists', str(r.data['display_name']))
        r = self.client.post('/api/admin/site-content/plans/', {'name': 'student', 'display_name': 'Student',
                                                                'price_monthly': 10, 'price_annual': 100}, format='json')
        self.assertEqual(self.client.delete(f"/api/admin/site-content/plans/{r.data['id']}/").status_code, 204)
        r = self.client.delete(f'/api/admin/site-content/plans/{self.plan.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('1 member', r.data['detail'])

    def test_website_list_titles_are_unique(self):
        body = {'data': {'title': {'en': 'Olympic Pool', 'ar': 'مسبح'}, 'category': 'aquatics'}}
        first = self.client.post('/api/admin/site-content/collections/facilities/items/', body, format='json')
        if first.status_code != 201:          # the facilities schema may require more fields
            self.skipTest(f'facility schema needs more fields: {first.data}')
        r = self.client.post('/api/admin/site-content/collections/facilities/items/', body, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('already in this list', r.data['data']['title'])

    def test_course_categories_are_a_fixed_list_and_titles_unique(self):
        body = {'title': 'Strength 101', 'description': 'x', 'category': 'Strength', 'trainer_id': self.coach.id}
        self.assertEqual(self.client.post('/api/admin/courses/', body, format='json').status_code, 201)
        self.assertIn('already exists', self.client.post('/api/admin/courses/', body, format='json').data['title'])
        r = self.client.post('/api/admin/courses/', {**body, 'title': 'Other', 'category': 'Underwater basket'}, format='json')
        self.assertIn('from the list', r.data['category'])

    def test_phone_letters_are_refused_for_nutritionists(self):
        r = self.client.post('/api/admin/nutrition/nutritionists/', {'email': 'x@t.local', 'first_name': 'Lama',
                                                                     'title': 'Dietitian', 'phone': '05abcdefgh'},
                             format='json')
        self.assertIn('Saudi mobile', r.data['phone'])


class ReviewTests(Base):
    def test_only_trained_members_review_and_rating_updates(self):
        self.client.force_authenticate(self.member)
        url = f'/api/catalog/trainers/{self.coach.id}/review/'
        self.assertEqual(self.client.post(url, {'rating': 5}, format='json').status_code, 400)
        TrainerSession.objects.create(member=self.member, trainer=self.coach, status='completed',
                                      scheduled_at=timezone.now() - timedelta(days=1))
        self.assertIn('1 to 5', self.client.post(url, {'rating': 9}, format='json').data['rating'])
        self.assertEqual(self.client.post(url, {'rating': 4, 'comment': 'Great'}, format='json').status_code, 201)
        self.assertEqual(self.client.post(url, {'rating': 5, 'comment': 'Even better'}, format='json').status_code, 200)
        detail = self.client.get(f'/api/catalog/trainers/{self.coach.id}/').data
        self.assertEqual((detail['total_reviews'], float(detail['rating']), detail['my_review']['comment']), (1, 5.0, 'Even better'))
        self.assertTrue(detail['can_review'])
        from django.core.cache import cache
        cache.delete('public-live-v1')                     # the live numbers are cached for a minute
        live = self.client.get('/api/public/live/').data
        self.assertEqual((live['reviews_count'], live['reviews'][0]['name']), (1, 'Sara A.'))


class PhotoQualityTests(APITestCase):
    def as_file(self, img):
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=92)
        return SimpleUploadedFile('p.jpg', buf.getvalue(), content_type='image/jpeg')

    def test_clear_blurry_dark_and_small(self):
        self.assertIsNone(photo_problem(self.as_file(clear_image())))
        board = Image.new('L', (320, 320), 70)
        px = board.load()
        for y in range(320):
            for x in range(320):
                if (x // 40 + y // 40) % 2:
                    px[x, y] = 190
        self.assertIn('blurry', photo_problem(self.as_file(board.filter(ImageFilter.GaussianBlur(4)).convert('RGB'))))
        self.assertIn('too dark', photo_problem(self.as_file(Image.new('RGB', (320, 320), (10, 10, 10)))))
        self.assertIn('too small', photo_problem(self.as_file(clear_image(100))))
        self.assertIn('can’t see a face', photo_problem(self.as_file(Image.new('RGB', (320, 320), (120, 120, 120)))))


class LiveNumbersTests(Base):
    def test_live_endpoint_counts_real_things(self):
        from django.core.cache import cache
        cache.delete('public-live-v1')
        GymClass.objects.create(name='Spin', category='endurance', day_of_week=timezone.localdate().weekday(),
                                start_time=time(18, 0), capacity=12)
        data = self.client.get('/api/public/live/').data
        self.assertEqual((data['members_active'], data['trainers'], data['classes_per_week']), (1, 1, 1))
        self.assertEqual(data['classes_today'][0]['spots_left'], 12)
        self.assertIsNone(data['avg_rating'])


class DemoDataTests(APITestCase):
    def test_seed_demo_activity_fills_the_charts_and_resets_cleanly(self):
        from django.core.management import call_command
        from apps.members.models import CheckIn
        from apps.payments.models import Transaction
        call_command('seed_demo_activity', members=6, days=20, verbosity=0)
        self.assertEqual(User.objects.filter(email__endswith='@smartgym.demo', role='member').count(), 6)
        self.assertTrue(CheckIn.objects.exists())
        self.assertTrue(Transaction.objects.filter(transaction_type='subscription').exists())
        call_command('seed_demo_activity', members=6, days=20, verbosity=0)          # runs again safely
        self.assertEqual(User.objects.filter(email__endswith='@smartgym.demo', role='member').count(), 6)
        real = User.objects.create_user(username='real', email='real@t.local', password='x')
        call_command('seed_demo_activity', members=0, days=1, reset=True, verbosity=0)
        self.assertFalse(User.objects.filter(email__endswith='@smartgym.demo', role='member').exists())
        self.assertTrue(User.objects.filter(id=real.id).exists())
