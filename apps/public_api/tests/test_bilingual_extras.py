"""
Phase 3 of the bilingual data: product extras, credentials and specialties, class rooms, milestones,
workout templates, articles and facilities carry Arabic next to the English.
"""
from datetime import time
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.attendance.models import Milestone
from apps.catalog.models import TrainerProfile
from apps.classes.models import GymClass
from apps.nutrition.models import NutritionistProfile
from apps.shop.models import Product, ProductCategory

User = get_user_model()


class BilingualExtrasTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        self.client.force_authenticate(self.admin)

    def test_product_extras_and_category_description(self):
        cat = ProductCategory.objects.create(key='protein', name='Protein')
        p = Product.objects.create(category=cat, name='Whey Isolate', sku='PRO-0001', price=Decimal('100'), stock_quantity=5)
        r = self.client.patch(f'/api/admin/shop/products/{p.id}/', {
            'badge_top_ar': 'الأكثر مبيعًا', 'badge_secondary_ar': 'مفلتر على البارد', 'price_note_ar': 'للعلبة',
            'stock_label_ar': 'يُشحن خلال يومين', 'telemetry_ar': '25 جم بروتين\n30 حصة\n\n'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['badge_top_ar'], r.data['telemetry_ar']), ('الأكثر مبيعًا', ['25 جم بروتين', '30 حصة']))
        self.client.patch(f'/api/admin/shop/categories/{cat.id}/', {'description_ar': 'بروتين عالي الجودة'}, format='json')
        self.client.force_authenticate(self.member)
        data = self.client.get('/api/shop/products/').data
        row = next(x for x in (data if isinstance(data, list) else data['results']) if x['id'] == p.id)
        self.assertEqual((row['price_note_ar'], row['stock_label_ar'], row['category']['description_ar']),
                         ('للعلبة', 'يُشحن خلال يومين', 'بروتين عالي الجودة'))

    def test_trainer_credentials_and_specialties_with_the_arabic_comma(self):
        coach = User.objects.create_user(username='c', email='c@t.local', password='x', role='trainer', first_name='Omar')
        t = TrainerProfile.objects.create(user=coach, title='Coach')
        r = self.client.patch(f'/api/admin/trainers/{t.id}/', {'credentials_ar': 'مدرب معتمد',
                                                                 'specialties_ar': 'القوة، المرونة, إنقاص الوزن'}, format='json')
        self.assertEqual((r.data['credentials_ar'], r.data['specialties_ar']), ('مدرب معتمد', ['القوة', 'المرونة', 'إنقاص الوزن']))
        self.client.force_authenticate(coach)
        r = self.client.patch('/api/coach/me/', {'specialties_ar': ['القوة']}, format='json')
        self.assertEqual(r.data['specialties_ar'], ['القوة'])
        self.client.force_authenticate(self.member)
        row = next(x for x in self.client.get('/api/catalog/trainers/').data if x['id'] == t.id)
        self.assertEqual((row['credentials_ar'], row['specialties_ar']), ('مدرب معتمد', ['القوة']))

    def test_nutritionist_and_class_room(self):
        u = User.objects.create_user(username='n', email='n@t.local', password='x', role='nutritionist', first_name='Huda')
        n = NutritionistProfile.objects.create(user=u, title='Dietitian', hourly_rate=0)
        r = self.client.patch(f'/api/admin/nutrition/nutritionists/{n.id}/', {'credentials_ar': 'أخصائية معتمدة',
                                                                                'specialties_ar': 'تغذية رياضية'}, format='json')
        self.assertEqual((r.data['credentials_ar'], r.data['specialties_ar']), ('أخصائية معتمدة', ['تغذية رياضية']))
        c = GymClass.objects.create(name='Spin', category='hiit', day_of_week=1, start_time=time(8), duration_minutes=45,
                                    capacity=10, location='Studio 1')
        r = self.client.patch(f'/api/classes/admin/{c.id}/', {'location_ar': 'الاستوديو 1'}, format='json')
        self.assertEqual(r.data['location_ar'], 'الاستوديو 1')

    def test_seeded_milestones_get_their_arabic(self):
        # The data migration ran on the test database; a newly seeded row gets it from the seed command.
        from django.core.management import call_command
        call_command('seed_milestones', stdout=StringIO())
        m = Milestone.objects.get(reward_code='CRYO-30')
        self.assertEqual((m.title_ar, m.reward_value_ar), ('30 يومًا بلا توقف', 'جلسة تبريد بقيمة 65 دولارًا'))
        self.client.force_authenticate(self.member)
        data = self.client.get('/api/attendance/milestones/').data
        rows = data if isinstance(data, list) else data.get('results', data.get('milestones', []))
        self.assertIn('نادي المئة يوم', [x['title_ar'] for x in rows])
