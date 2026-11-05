"""The member shop leaves out products that are out of stock; the admin shop still lists them."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.shop.models import Product, ProductCategory

User = get_user_model()


class OutOfStockTests(APITestCase):
    def setUp(self):
        cat = ProductCategory.objects.create(key='protein', name='Protein')
        self.whey = Product.objects.create(category=cat, name='Whey', sku='PRO-0001', price=Decimal('100'), stock_quantity=4)
        self.bar = Product.objects.create(category=cat, name='Bar', sku='PRO-0002', price=Decimal('10'), stock_quantity=0)
        self.old = Product.objects.create(category=cat, name='Old', sku='PRO-0003', price=Decimal('10'), stock_quantity=-2)
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')

    def names(self, url):
        data = self.client.get(url).data
        return sorted(p['name'] for p in (data if isinstance(data, list) else data['results']))

    def test_member_shop_hides_out_of_stock_by_default(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.names('/api/shop/products/'), ['Whey'])
        self.assertEqual(self.names('/api/shop/products/?in_stock=true'), ['Whey'])
        self.assertEqual(self.names('/api/shop/products/?in_stock=false'), ['Bar', 'Old', 'Whey'])
        # A saved link to a sold-out product still opens (it says "Out of stock").
        r = self.client.get(f'/api/shop/products/{self.bar.id}/')
        self.assertEqual((r.status_code, r.data['is_out_of_stock']), (200, True))

    def test_back_in_stock_shows_again(self):
        self.client.force_authenticate(self.member)
        Product.objects.filter(pk=self.bar.pk).update(stock_quantity=3)
        self.assertEqual(self.names('/api/shop/products/'), ['Bar', 'Whey'])

    def test_admin_shop_lists_everything(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.names('/api/admin/shop/products/'), ['Bar', 'Old', 'Whey'])
