"""The "Missing Arabic" list: records with English text but no Arabic, as a command and for the admin page."""
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from rest_framework.test import APITestCase

from apps.attendance.models import Milestone
from apps.shop.models import Product, ProductCategory
from apps.site_content.models import SiteItem, SiteText

User = get_user_model()


class MissingArabicTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        cat = ProductCategory.objects.create(key='protein', name='Protein', name_ar='بروتين')
        self.whey = Product.objects.create(category=cat, name='Whey', name_ar='واي', description='Fast protein',
                                           sku='PRO-0001', price=Decimal('100'), stock_quantity=1,
                                           telemetry=['25g protein'], telemetry_ar=[])
        Product.objects.create(category=cat, name='Bar', name_ar='لوح', sku='PRO-0002', price=Decimal('5'))   # complete
        SiteText.objects.update_or_create(key='home.test.only', defaults={'page': 'home', 'en': 'Hello', 'ar': ''})
        self.item = SiteItem.objects.create(collection='facilities', data={'title': {'en': 'Pool', 'ar': ''}})
        Milestone.objects.filter(title_ar='').delete()

    def group(self, data, key):
        return next(g for g in data['groups'] if g['key'] == key)

    def test_admin_api_lists_what_is_missing(self):
        self.client.force_authenticate(self.admin)
        data = self.client.get('/api/admin/missing-arabic/').data
        products = self.group(data, 'products')
        self.assertEqual(products['count'], 1)
        self.assertEqual(products['items'][0], {'id': self.whey.id, 'name': 'Whey', 'fields': ['description', 'telemetry'],
                                                'link': f'/admin/shop?highlight={self.whey.id}', 'edit_url': ''})
        self.assertEqual(self.group(data, 'categories')['count'], 0)
        self.assertIn('home.test.only', [i['id'] for i in self.group(data, 'site_texts')['items']])
        lists = self.group(data, 'site_lists')['items']
        self.assertIn({'id': self.item.id, 'name': 'facilities: Pool', 'fields': ['title'],
                       'link': f'/admin/site-lists?list=facilities&edit={self.item.id}', 'edit_url': ''}, lists)
        self.assertEqual(data['total'], sum(g['count'] for g in data['groups']))

    def test_django_admin_link_for_records_without_a_portal_page(self):
        m = Milestone.objects.create(target_days=7, tier='Tier 00', title='First week', reward_code='WEEK-7')
        self.client.force_authenticate(self.admin)
        row = self.group(self.client.get('/api/admin/missing-arabic/').data, 'milestones')['items'][0]
        self.assertTrue(row['edit_url'].endswith(f'/django-admin/attendance/milestone/{m.id}/change/'))
        self.assertIn('title', row['fields'])

    def test_members_cannot_see_it_and_the_command_prints_it(self):
        member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        self.client.force_authenticate(member)
        self.assertEqual(self.client.get('/api/admin/missing-arabic/').status_code, 403)
        out = StringIO()
        call_command('list_missing_arabic', stdout=out)
        self.assertIn('Shop products (products): 1', out.getvalue())
        self.assertIn(f'#{self.whey.id}  Whey  -  missing: description, telemetry', out.getvalue())
        out = StringIO()
        call_command('list_missing_arabic', '--counts', '--only', 'categories', stdout=out)
        self.assertIn('Everything has Arabic.', out.getvalue())
