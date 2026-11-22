"""Shop manager portal: orders directly, catalog changes wait for an admin, refunds stay with admins."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.approvals.models import ChangeRequest
from apps.approvals.registry import approve, reject
from apps.shop.models import Order, Product, ProductCategory, PromoCode

User = get_user_model()


class ShopPortalTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        self.shop = User.objects.create_user(username='sh', email='sh@t.local', password='x', role='shop_manager')
        self.member = User.objects.create_user(username='mm', email='mm@t.local', password='x')
        self.cat = ProductCategory.objects.create(key='protein', name='Protein')
        self.live = Product.objects.create(category=self.cat, name='Whey One', sku='PRO-0001', price=Decimal('100'),
                                           stock_quantity=10)
        self.client.force_authenticate(self.shop)

    def member_sees(self):
        self.client.force_authenticate(self.member)
        names = [p['name'] for p in self.client.get('/api/shop/products/').data]
        self.client.force_authenticate(self.shop)
        return names

    def test_new_product_waits_for_the_admin(self):
        r = self.client.post('/api/admin/shop/products/', {'name': 'Creatine Max', 'price': '80', 'category_id': self.cat.id,
                                                           'stock_quantity': 5}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['approval_status'], 'pending')
        self.assertEqual(r.data['pending'][0]['kind'], 'create')
        Product.objects.filter(pk=r.data['id']).update(stock_quantity=5)
        self.assertNotIn('Creatine Max', self.member_sees())
        approve(ChangeRequest.objects.get(object_id=r.data['id'], target_type='shop.product'), self.admin)
        self.assertIn('Creatine Max', self.member_sees())

    def test_price_change_keeps_the_live_price_until_approved_but_stock_is_direct(self):
        r = self.client.patch(f'/api/admin/shop/products/{self.live.id}/', {'price': '120', 'stock_quantity': 25},
                              format='json')
        self.assertEqual(r.status_code, 202, r.data)
        self.live.refresh_from_db()
        self.assertEqual((self.live.price, self.live.stock_quantity), (Decimal('100.00'), 25))
        cr = ChangeRequest.objects.get(target_type='shop.product', kind='update')
        approve(cr, self.admin, payload={'price': 110})            # the admin adjusts the price before approving
        self.live.refresh_from_db()
        self.assertEqual(self.live.price, Decimal('110.00'))

    def test_rejected_new_item_can_be_fixed_and_sent_again(self):
        r = self.client.post('/api/admin/shop/promos/', {'code': 'SUMMER', 'discount_percent': 10}, format='json')
        promo = PromoCode.objects.get(code='SUMMER')
        self.assertFalse(promo.is_valid)                          # not usable at checkout yet
        reject(ChangeRequest.objects.get(object_id=r.data['id'], target_type='shop.promo'), self.admin, note='Too much')
        promo.refresh_from_db()
        self.assertEqual(promo.approval_status, 'rejected')
        r = self.client.patch(f'/api/admin/shop/promos/{promo.id}/', {'discount_percent': 5}, format='json')
        self.assertEqual(r.data['approval_status'], 'pending')
        self.assertEqual(ChangeRequest.objects.filter(target_type='shop.promo', status='pending').count(), 1)

    def test_removing_a_live_product_waits(self):
        r = self.client.delete(f'/api/admin/shop/products/{self.live.id}/')
        self.assertEqual(r.status_code, 202)
        self.assertTrue(Product.objects.filter(pk=self.live.pk).exists())
        approve(ChangeRequest.objects.get(target_type='shop.product', kind='delete'), self.admin)
        self.assertFalse(Product.objects.filter(pk=self.live.pk).exists())

    def test_orders_direct_refunds_admin_only(self):
        order = Order.objects.create(order_number='ORD-T1', member=self.member, status='paid', total=Decimal('115'))
        self.assertEqual(self.client.get('/api/admin/shop/orders/').status_code, 200)
        r = self.client.post(f'/api/admin/shop/orders/{order.order_number}/status/', {'status': 'preparing'}, format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(self.client.post(f'/api/admin/shop/orders/{order.order_number}/refund/', {}, format='json').status_code, 403)

    def test_admin_changes_apply_at_once(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f'/api/admin/shop/products/{self.live.id}/', {'price': '90'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.live.refresh_from_db()
        self.assertEqual(self.live.price, Decimal('90.00'))
        self.assertFalse(ChangeRequest.objects.exists())

    def test_other_roles_cannot_open_the_shop_back_office(self):
        desk = User.objects.create_user(username='rc', email='rc@t.local', password='x', role='receptionist')
        self.client.force_authenticate(desk)
        self.assertEqual(self.client.get('/api/admin/shop/products/').status_code, 403)
