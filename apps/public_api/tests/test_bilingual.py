"""
Bilingual staff-entered data: admins (and coaches, for their own profile and courses) save the
Arabic next to the English, and every member / public API returns both, so the portal can show the
reader's language and fall back to the English when the Arabic is empty.
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.catalog.models import Course, TrainerProfile
from apps.classes.models import GymClass
from apps.nutrition.models import NutritionistProfile
from apps.shop.models import Product, ProductCategory
from apps.workouts.models import Exercise

User = get_user_model()


class BilingualDataTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        self.coach = User.objects.create_user(username='c', email='c@t.local', password='x', role='trainer',
                                              first_name='Omar', last_name='Saleh')
        self.trainer = TrainerProfile.objects.create(user=self.coach, title='Strength Coach')
        self.cat = ProductCategory.objects.create(key='protein', name='Protein')
        self.product = Product.objects.create(category=self.cat, name='Whey Isolate', sku='PRO-0001',
                                              price=Decimal('100'), stock_quantity=5)
        self.client.force_authenticate(self.admin)

    def test_product_and_category_arabic_round_trip(self):
        r = self.client.patch(f'/api/admin/shop/products/{self.product.id}/',
                              {'name_ar': '  واي أيزوليت ', 'description_ar': 'بروتين سريع الامتصاص'}, format='json')
        self.assertEqual((r.status_code, r.data['name_ar'], r.data['name']), (200, 'واي أيزوليت', 'Whey Isolate'))
        r = self.client.patch(f'/api/admin/shop/categories/{self.cat.id}/', {'name_ar': 'بروتين'}, format='json')
        self.assertEqual(r.data['name_ar'], 'بروتين')
        r = self.client.post('/api/admin/shop/categories/', {'name': 'Gear', 'name_ar': 'معدات'}, format='json')
        self.assertEqual(r.data['name_ar'], 'معدات')
        self.client.force_authenticate(self.member)
        data = self.client.get('/api/shop/products/').data
        row = next(p for p in (data if isinstance(data, list) else data['results']) if p['id'] == self.product.id)
        self.assertEqual((row['name_ar'], row['description_ar'], row['category']['name_ar']),
                         ('واي أيزوليت', 'بروتين سريع الامتصاص', 'بروتين'))

    def test_arabic_is_optional_and_length_checked(self):
        r = self.client.patch(f'/api/admin/shop/products/{self.product.id}/', {'name_ar': 'ب' * 201}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('200', r.data['name_ar'])
        self.assertEqual(Product.objects.get(pk=self.product.pk).name_ar, '')     # untouched, English shown

    def test_class_machine_course_and_people(self):
        c = GymClass.objects.create(name='Morning HIIT', category='hiit', day_of_week=0, start_time=time(7),
                                    duration_minutes=45, capacity=10)
        r = self.client.patch(f'/api/classes/admin/{c.id}/', {'name_ar': 'هيت الصباح', 'description_ar': 'تمارين عالية الشدة'},
                              format='json')
        self.assertEqual((r.status_code, r.data['name_ar']), (200, 'هيت الصباح'))

        ex = Exercise.objects.create(name='Leg Press', category='strength', primary_muscle='legs')
        r = self.client.patch(f'/api/admin/machines/{ex.id}/', {'name_ar': 'ضغط الأرجل', 'setup_instructions_ar': 'اضبط المقعد'},
                              format='json')
        self.assertEqual((r.data['name_ar'], r.data['setup_instructions_ar']), ('ضغط الأرجل', 'اضبط المقعد'))

        course = Course.objects.create(trainer=self.trainer, title='Strength 101', description='x', category='Strength')
        r = self.client.patch(f'/api/admin/courses/{course.id}/', {'title_ar': 'أساسيات القوة', 'subtitle_ar': 'ابدأ هنا'},
                              format='json')
        self.assertEqual(r.data['title_ar'], 'أساسيات القوة')
        Course.objects.filter(pk=course.pk).update(status='published')

        r = self.client.patch(f'/api/admin/trainers/{self.trainer.id}/', {'title_ar': 'مدرب قوة', 'bio_ar': 'خبرة عشر سنوات'},
                              format='json')
        self.assertEqual((r.data['title_ar'], r.data['bio_ar']), ('مدرب قوة', 'خبرة عشر سنوات'))
        n_user = User.objects.create_user(username='n', email='n@t.local', password='x', role='nutritionist', first_name='Huda')
        n = NutritionistProfile.objects.create(user=n_user, title='Dietitian', hourly_rate=0)
        r = self.client.patch(f'/api/admin/nutrition/nutritionists/{n.id}/', {'bio_ar': 'أخصائية تغذية'}, format='json')
        self.assertEqual(r.data['bio_ar'], 'أخصائية تغذية')

        # members and the public site get both languages
        self.client.force_authenticate(self.member)
        detail = self.client.get(f'/api/catalog/courses/{course.id}/').data
        self.assertEqual((detail['title_ar'], detail['subtitle_ar'], detail['trainer']['title_ar']),
                         ('أساسيات القوة', 'ابدأ هنا', 'مدرب قوة'))
        public = self.client.get('/api/public/trainers/').data
        rows = public if isinstance(public, list) else public.get('results', [])
        self.assertEqual(next(t for t in rows if t['id'] == self.trainer.id)['bio_ar'], 'خبرة عشر سنوات')
        classes = self.client.get('/api/classes/').data
        rows = classes if isinstance(classes, list) else classes.get('results', classes.get('classes', []))
        self.assertEqual(next(x for x in rows if x['id'] == c.id)['name_ar'], 'هيت الصباح')

    def test_coach_writes_arabic_but_not_a_live_courses_title(self):
        self.client.force_authenticate(self.coach)
        r = self.client.patch('/api/coach/me/', {'bio_ar': 'أحب تمارين القوة'}, format='json')
        self.assertEqual((r.status_code, r.data['bio_ar']), (200, 'أحب تمارين القوة'))
        course = Course.objects.create(trainer=self.trainer, title='Live', description='x', category='Strength',
                                       status='published')
        r = self.client.patch(f'/api/coach/courses/{course.id}/', {'title_ar': 'مباشر'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('title_ar', r.data)
        r = self.client.patch(f'/api/coach/courses/{course.id}/', {'description_ar': 'وصف'}, format='json')
        self.assertEqual(r.status_code, 200)


class BilingualLessonsAndVideosTests(APITestCase):
    """Phase 2: course lessons and machine videos carry Arabic too."""

    def setUp(self):
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        self.coach = User.objects.create_user(username='c', email='c@t.local', password='x', role='trainer',
                                              first_name='Omar', last_name='Saleh')
        self.trainer = TrainerProfile.objects.create(user=self.coach, title='Strength Coach')
        self.machine = Exercise.objects.create(name='Leg Press', category='strength', primary_muscle='legs')

    def test_lessons_saved_by_the_coach_reach_members(self):
        course = Course.objects.create(trainer=self.trainer, title='Strength 101', description='x', category='Strength')
        self.client.force_authenticate(self.coach)
        r = self.client.patch(f'/api/coach/courses/{course.id}/', {'modules': [
            {'title': 'Squat basics', 'title_ar': 'أساسيات القرفصاء', 'description': 'Stance', 'description_ar': 'وضعية القدمين',
             'week_number': 1, 'day_number': 1, 'duration_minutes': 20}]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['modules'][0]['title_ar'], 'أساسيات القرفصاء')
        r = self.client.patch(f'/api/coach/courses/{course.id}/', {'modules': [
            {'title': 'Squat basics', 'title_ar': 'ق' * 201, 'week_number': 1, 'day_number': 1, 'duration_minutes': 20}]},
            format='json')
        self.assertEqual(r.status_code, 400)
        Course.objects.filter(pk=course.pk).update(status='published')
        self.client.force_authenticate(self.member)
        module = self.client.get(f'/api/catalog/courses/{course.id}/').data['modules'][0]
        self.assertEqual((module['title_ar'], module['description_ar']), ('أساسيات القرفصاء', 'وضعية القدمين'))

    def test_videos_saved_by_coach_and_admin_reach_members(self):
        self.client.force_authenticate(self.coach)
        r = self.client.post('/api/coach/videos/', {'exercise_id': self.machine.id, 'tier': 'basic', 'title': 'Seat setup',
                                                    'title_ar': 'ضبط المقعد', 'video_url': 'https://youtu.be/abc',
                                                    'duration_minutes': 3, 'description_ar': 'اضبط الارتفاع'}, format='json')
        self.assertEqual((r.status_code, r.data['title_ar']), (201, 'ضبط المقعد'))
        admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        self.client.force_authenticate(admin)
        r = self.client.patch(f"/api/admin/machine-videos/{r.data['id']}/", {'title_ar': 'ضبط مقعد الجهاز'}, format='json')
        self.assertEqual(r.data['title_ar'], 'ضبط مقعد الجهاز')
        self.client.force_authenticate(self.member)
        data = self.client.get(f'/api/workouts/exercises/{self.machine.id}/videos/').data
        rows = data['videos'] if isinstance(data, dict) else data
        self.assertEqual((rows[0]['title_ar'], rows[0]['description_ar']), ('ضبط مقعد الجهاز', 'اضبط الارتفاع'))
