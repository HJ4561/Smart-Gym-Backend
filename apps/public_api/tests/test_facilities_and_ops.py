"""Facility add-ons (signup + mid-membership) and the admin back office (pricing, orders, refunds, trainer hours)."""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.catalog.models import TrainerAvailability, TrainerProfile, TrainerSession
from apps.members.models import FacilityAddon, Membership, MembershipFacility, MembershipRegistration as R
from apps.members.services import approve_registration
from apps.payments.models import LockerCredit, PaymentIntent, Transaction
from apps.public_api import moyasar
from apps.shop.models import Cart, CartItem, Order, Product, ProductCategory
from apps.site_content.models import SiteItem

from .test_member_portal import PortalBase
from .test_payments import PaymentTestBase
from .faces import one_face

# Test photos have no real face — stub the detector to find one well-placed face.
_faces = one_face()


def setUpModule():
    _faces.start()


def tearDownModule():
    _faces.stop()

User = get_user_model()


def facility(title, price, bookable=True):
    item = SiteItem.objects.create(collection='facilities', data={'title': {'en': title, 'ar': title}, 'category': 'aquatics'})
    FacilityAddon.objects.create(item=item, monthly_price=Decimal(price), is_bookable=bookable)
    return item


class SignupFacilityTests(PaymentTestBase):
    def setUp(self):
        super().setUp()
        SiteItem.objects.filter(collection='facilities').delete()      # seeded items have no price
        self.pool = facility('Pool', '50')
        self.closed = facility('Courts', '40', bookable=False)

    def test_public_list_carries_price(self):
        rows = {r['id']: r for r in self.client.get('/api/public/collections/facilities/').data}
        self.assertEqual((rows[self.pool.id]['monthly_price'], rows[self.pool.id]['bookable']), ('50.00', True))
        self.assertFalse(rows[self.closed.id]['bookable'])

    def test_signup_charges_plan_plus_facilities_and_grants_access(self):
        r = self.register(facilities=str(self.pool.id))          # 3 months of Pro (79) + pool 50 x 3
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['amount'], '387.00')
        self.assertEqual(r.data['payment']['amount'], 38700)
        reg = self.reg(r.data['id'])
        self.pay('pay_f', reg.id, 38700)
        self.client.post('/api/public/payments/moyasar/verify/', {'payment_id': 'pay_f'}, format='json')
        user = approve_registration(self.reg(reg.id))
        access = MembershipFacility.objects.get(member=user)
        self.assertEqual((access.facility_name, access.months, access.source), ('Pool', 3, 'signup'))
        self.assertEqual(access.expires_on, Membership.objects.get(member=user).expiry_date)
        self.assertIn('1 facility', Transaction.objects.get(member=user).description)

    def test_unbookable_facility_is_refused(self):
        r = self.register(facilities=f'{self.pool.id},{self.closed.id}')
        self.assertEqual(r.status_code, 400)
        self.assertIn('facilities', r.data)


@override_settings(LOCKER_CREDIT_ENABLED=True)   # tests the locker credit feature itself
class AddonTests(PortalBase):
    def setUp(self):
        super().setUp()
        self.pool = facility('Pool', '50')
        # 30-day membership from PortalBase → 1 month remaining (a started month counts)

    def test_member_sees_prorated_price_and_buys_with_card(self):
        rows = {r['id']: r for r in self.client.get('/api/members/facilities/').data}
        self.assertEqual(rows[self.pool.id]['addon_price'], '50.00')
        pay = self.checkout(purpose='facility_addon', facility_id=self.pool.id)
        self.assertEqual(pay.data['amount'], '50.00')
        self.assertEqual(self.moyasar_pays(pay.data).data['status'], 'paid')
        access = MembershipFacility.objects.get(member=self.user)
        self.assertEqual((access.source, access.expires_on), ('addon', self.membership.expiry_date))
        self.assertTrue(self.client.get('/api/members/facilities/').data[0]['owned'])
        self.assertEqual(self.checkout(purpose='facility_addon', facility_id=self.pool.id).status_code, 400)

    def test_buy_with_locker_credit(self):
        LockerCredit.objects.create(member=self.user, balance=Decimal('60'))
        r = self.checkout(purpose='facility_addon', facility_id=self.pool.id, method='locker_credit')
        self.assertEqual(r.data['status'], 'paid')
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('10.00'))

    def test_months_remaining_drives_price(self):
        self.membership.expiry_date = date.today() + timedelta(days=95)    # 3 months + days → 4
        self.membership.save()
        self.assertEqual(self.checkout(purpose='facility_addon', facility_id=self.pool.id).data['amount'], '200.00')

    def test_expired_membership_cannot_add(self):
        self.membership.expiry_date = date.today() - timedelta(days=1)
        self.membership.save()
        self.assertEqual(self.checkout(purpose='facility_addon', facility_id=self.pool.id).status_code, 400)


@override_settings(LOCKER_CREDIT_ENABLED=True)   # tests the locker credit feature itself
class AdminOpsTests(PortalBase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(username='adm', email='adm@test.local', password='x', role='admin')
        cat = ProductCategory.objects.create(key='s', name='S')
        self.product = Product.objects.create(category=cat, name='Whey', sku='W', price=Decimal('100'), stock_quantity=5)

    def paid_order(self, credit=True):
        cart = Cart.objects.create(member=self.user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        num = self.client.post('/api/shop/checkout/', {}, format='json').data['order_number']
        if credit:
            LockerCredit.objects.create(member=self.user, balance=Decimal('200'), max_cap=Decimal('300'))
            self.checkout(purpose='shop_order', order_number=num, method='locker_credit')
        else:
            self.moyasar_pays(self.checkout(purpose='shop_order', order_number=num).data, pid='pay_o')
        return num

    def test_members_cannot_use_admin_ops(self):
        self.assertEqual(self.client.get('/api/admin/shop/orders/').status_code, 403)

    def test_facility_pricing(self):
        item = SiteItem.objects.create(collection='facilities', data={'title': {'en': 'Sauna'}})
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.patch(f'/api/admin/facility-pricing/{item.id}/', {'is_bookable': True}, format='json').status_code, 400)
        r = self.client.patch(f'/api/admin/facility-pricing/{item.id}/', {'monthly_price': '35', 'is_bookable': True}, format='json')
        self.assertEqual((r.data['monthly_price'], r.data['is_bookable']), ('35.00', True))
        self.assertIn(item.id, [row['id'] for row in self.client.get('/api/admin/facility-pricing/').data['results']])

    def test_order_flow_and_credit_refund(self):
        num = self.paid_order()
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(f'/api/admin/shop/orders/{num}/status/', {'status': 'completed'}).status_code, 400)
        self.assertEqual(self.client.post(f'/api/admin/shop/orders/{num}/status/', {'status': 'ready'}).data['status'], 'ready')
        r = self.client.post(f'/api/admin/shop/orders/{num}/refund/', {'reason': 'damaged'}, format='json')
        self.assertEqual(r.data['status'], 'refunded')
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('200.00'))
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 5)

    def test_card_refund_calls_moyasar(self):
        num = self.paid_order(credit=False)
        self.client.force_authenticate(self.admin)
        with mock.patch.object(moyasar, 'refund_payment', return_value={'status': 'refunded'}) as refund:
            r = self.client.post(f'/api/admin/shop/orders/{num}/refund/', {}, format='json')
        self.assertEqual(r.status_code, 200)
        refund.assert_called_once_with('pay_o', 11500)
        self.assertEqual(PaymentIntent.objects.get(order__order_number=num).status, 'refunded')

    def test_review_queue_refund(self):
        intent_data = self.checkout(purpose='locker_topup', amount=50).data
        self.moyasar_pays(intent_data, amount=100)           # wrong amount → review
        self.client.force_authenticate(self.admin)
        self.assertEqual(len(self.client.get('/api/admin/payments/review/').data), 1)
        with mock.patch.object(moyasar, 'refund_payment', return_value={}):
            r = self.client.post(f"/api/admin/payments/intents/{intent_data['id']}/refund/", {}, format='json')
        self.assertEqual(r.data['status'], 'refunded')

    def test_refunding_a_topup_takes_the_credit_back(self):
        from apps.payments import gateway
        data = self.checkout(purpose='locker_topup', amount=50).data
        self.moyasar_pays(data)
        intent = PaymentIntent.objects.get(id=data['id'])
        with mock.patch.object(moyasar, 'refund_payment', return_value={}):
            gateway.refund_intent(intent)
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('0.00'))

    def test_trainer_hours_feed_slots(self):
        coach = User.objects.create_user(username='c', email='c@test.local', password='x')
        t = TrainerProfile.objects.create(user=coach, title='Coach')
        self.client.force_authenticate(self.admin)
        bad = self.client.put(f'/api/admin/trainer-hours/{t.id}/', {'windows': [
            {'day_of_week': 1, 'start_time': '09:00', 'end_time': '12:00'},
            {'day_of_week': 1, 'start_time': '11:00', 'end_time': '13:00'}]}, format='json')
        self.assertEqual(bad.status_code, 400)
        day = date.today() + timedelta(days=3)
        r = self.client.put(f'/api/admin/trainer-hours/{t.id}/', {'hourly_rate': '150', 'windows': [
            {'day_of_week': day.weekday(), 'start_time': '09:00', 'end_time': '11:00'}]}, format='json')
        self.assertEqual((r.data['hourly_rate'], len(r.data['windows'])), ('150.00', 1))
        self.client.force_authenticate(self.user)
        slots = self.client.get(f'/api/catalog/trainers/{t.id}/slots/', {'date': day.isoformat()}).data
        self.assertEqual(([s['label'] for s in slots['slots']], slots['price']), (['09:00', '10:00'], '150.00'))

    def test_staff_cancel_refunds_to_credit(self):
        coach = User.objects.create_user(username='c', email='c@test.local', password='x')
        t = TrainerProfile.objects.create(user=coach, title='Coach')
        from django.utils import timezone
        s = TrainerSession.objects.create(member=self.user, trainer=t, status='scheduled', amount_charged=Decimal('120'),
                                          scheduled_at=timezone.now() + timedelta(days=2))
        self.client.force_authenticate(self.admin)
        self.assertEqual(len(self.client.get('/api/admin/trainer-sessions/').data), 1)
        self.client.post(f'/api/admin/trainer-sessions/{s.id}/status/', {'status': 'cancelled'})
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('120.00'))


class StaleOrderTests(APITestCase):
    """Unpaid orders give their stock back after 30 minutes; a late card payment takes it again."""

    def setUp(self):
        from apps.shop.models import Order, OrderItem, Product, ProductCategory
        self.member = User.objects.create_user(username='so', email='so@t.local', password='x', role='member')
        cat = ProductCategory.objects.create(key='p', name='P')
        self.product = Product.objects.create(category=cat, name='Bar', sku='BAR', price=Decimal('10'), stock_quantity=4)
        self.order = Order.objects.create(order_number='ORD-OLD', member=self.member, status='pending',
                                          subtotal=Decimal('20'), total=Decimal('20'))
        OrderItem.objects.create(order=self.order, product=self.product, product_name='Bar', product_sku='BAR',
                                 unit_price=Decimal('10'), quantity=2, line_total=Decimal('20'))
        Product.objects.filter(id=self.product.id).update(stock_quantity=2)       # reserved at checkout
        Order.objects.filter(id=self.order.id).update(created_at=timezone.now() - timedelta(minutes=45))

    def test_expiry_releases_stock_and_late_payment_retakes_it(self):
        from apps.payments.gateway import _fulfil_order
        from apps.payments.models import PaymentIntent
        from apps.shop.services import expire_stale_orders
        self.assertEqual(expire_stale_orders(), 1)
        self.product.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual((self.product.stock_quantity, self.order.status), (4, 'cancelled'))
        intent = PaymentIntent(member=self.member, purpose='shop_order', amount=Decimal('20'), order=self.order)
        ok, _ = _fulfil_order(intent)
        self.order.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual((ok, self.order.status, self.product.stock_quantity), (True, 'paid', 2))
