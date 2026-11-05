"""
Member portal: payments (Moyasar + locker credit), shop orders, trainer
bookings, paid courses, renewals, digital pass QR, check-in rules, attendance
and dashboard data.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.attendance.models import MemberMilestone, Milestone
from apps.catalog.models import Course, Enrollment, TrainerAvailability, TrainerProfile, TrainerSession
from apps.members.models import CheckIn, Membership, MembershipPlan
from apps.members.passes import make_token
from apps.notifications.models import Notification
from apps.payments.models import Invoice, LockerCredit, PaymentIntent, Transaction
from apps.public_api import moyasar
from apps.shop.models import Cart, CartItem, Order, Product, ProductCategory, PromoCode

User = get_user_model()
WEBHOOK_SECRET = 'whsec_test'


@override_settings(MOYASAR_SECRET_KEY='sk_test_dummy', MOYASAR_PUBLISHABLE_KEY='pk_test_dummy',
                   MOYASAR_WEBHOOK_SECRET=WEBHOOK_SECRET)
class PortalBase(APITestCase):
    def setUp(self):
        self.payments = {}
        patcher = mock.patch.object(moyasar, 'fetch_payment', side_effect=self.fake_fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.plan = MembershipPlan.objects.create(name='pro', display_name='Pro', price_monthly=79, price_annual=790)
        self.user = User.objects.create_user(username='m', email='m@test.local', password='x', role='member',
                                             first_name='Sara', last_name='Ali')
        today = date.today()
        self.membership = Membership.objects.create(member=self.user, plan=self.plan, start_date=today,
                                                    expiry_date=today + timedelta(days=30), iqama_number='2111111111')
        self.client.force_authenticate(self.user)

    def fake_fetch(self, pid):
        if pid not in self.payments:
            raise moyasar.MoyasarError('Payment not found.')
        return self.payments[pid]

    def checkout(self, **body):
        return self.client.post('/api/payments/checkout/', body, format='json')

    def moyasar_pays(self, intent_data, pid='pay_1', status='paid', amount=None, attempt=None):
        intent = PaymentIntent.objects.get(id=intent_data['id'])
        self.payments[pid] = {
            'id': pid, 'status': status, 'currency': 'SAR',
            'amount': intent.amount_halalas if amount is None else amount,
            'metadata': {'intent_id': str(intent.id), 'attempt': attempt or intent.attempt_token},
            'source': {'type': 'creditcard', 'company': 'mada', 'number': '4201-32XX-XXXX-1010'},
        }
        return self.client.post(f'/api/payments/intents/{intent.id}/attach/', {'payment_id': pid}, format='json')


@override_settings(LOCKER_CREDIT_ENABLED=True)   # tests the locker credit feature itself
class TopUpTests(PortalBase):
    def test_free_topup_endpoint_is_gone(self):
        self.assertEqual(self.client.post('/api/payments/locker-credit/topup/', {'amount': 50}).status_code, 404)

    def test_topup_credits_only_after_moyasar_confirms(self):
        r = self.checkout(purpose='locker_topup', amount=50)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['payment']['amount'], 5000)
        self.assertEqual(r.data['payment']['metadata']['intent_id'], str(r.data['id']))
        self.assertFalse(LockerCredit.objects.filter(member=self.user, balance__gt=0).exists())

        r2 = self.moyasar_pays(r.data)
        self.assertEqual(r2.data['status'], 'paid')
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('50.00'))
        txn = Transaction.objects.get(member=self.user)
        self.assertEqual((txn.transaction_type, txn.status, txn.payment_method_label), ('topup', 'settled', 'MADA •••• 1010'))
        self.assertTrue(Invoice.objects.filter(transaction=txn, status='paid').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.user, title='Locker credit added').exists())

        # Callback page and webhook arriving later don't credit twice.
        self.client.post('/api/payments/intents/verify/', {'payment_id': 'pay_1'}, format='json')
        self.client.post('/api/public/payments/moyasar/webhook/',
                         {'secret_token': WEBHOOK_SECRET, 'type': 'payment_paid', 'data': {'id': 'pay_1'}}, format='json')
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('50.00'))

    def test_wrong_amount_goes_to_review(self):
        r = self.checkout(purpose='locker_topup', amount=50)
        self.assertEqual(self.moyasar_pays(r.data, amount=100).data['status'], 'review')
        self.assertFalse(LockerCredit.objects.filter(member=self.user, balance__gt=0).exists())

    def test_forged_attempt_is_rejected(self):
        r = self.checkout(purpose='locker_topup', amount=50)
        self.assertEqual(self.moyasar_pays(r.data, attempt='forged').status_code, 400)

    def test_cap_and_minimum(self):
        self.assertEqual(self.checkout(purpose='locker_topup', amount=5).status_code, 400)
        self.assertEqual(self.checkout(purpose='locker_topup', amount=500).status_code, 400)   # cap 100

    def test_webhook_fulfils_without_callback(self):
        r = self.checkout(purpose='locker_topup', amount=20)
        intent = PaymentIntent.objects.get(id=r.data['id'])
        self.payments['pay_w'] = {'id': 'pay_w', 'status': 'paid', 'currency': 'SAR', 'amount': 2000,
                                  'metadata': {'intent_id': str(intent.id), 'attempt': intent.attempt_token}, 'source': {}}
        res = self.client.post('/api/public/payments/moyasar/webhook/',
                               {'secret_token': WEBHOOK_SECRET, 'data': {'id': 'pay_w'}}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('20.00'))

    def test_verify_refuses_other_members_payment(self):
        r = self.checkout(purpose='locker_topup', amount=20)
        self.moyasar_pays(r.data)
        other = User.objects.create_user(username='o', email='o@test.local', password='x')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post('/api/payments/intents/verify/', {'payment_id': 'pay_1'}, format='json').status_code, 400)


@override_settings(LOCKER_CREDIT_ENABLED=True)   # tests the locker credit feature itself
class ShopTests(PortalBase):
    def setUp(self):
        super().setUp()
        cat = ProductCategory.objects.create(key='supp', name='Supplements')
        self.product = Product.objects.create(category=cat, name='Whey', sku='W1', price=Decimal('100.00'), stock_quantity=3)
        self.cart = Cart.objects.create(member=self.user)

    def add(self, qty):
        CartItem.objects.create(cart=self.cart, product=self.product, quantity=qty)

    def test_checkout_reserves_stock_and_waits_for_payment(self):
        self.add(2)
        r = self.client.post('/api/shop/checkout/', {'delivery_method': 'counter'}, format='json')
        self.assertEqual(r.status_code, 201)
        order = Order.objects.get(order_number=r.data['order_number'])
        self.assertEqual(order.status, 'pending')
        self.assertEqual(order.total, Decimal('230.00'))          # 200 + 15% VAT
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 1)

        pay = self.checkout(purpose='shop_order', order_number=order.order_number)
        self.moyasar_pays(pay.data)
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid')

    def test_no_automatic_promo_and_invalid_promo_rejected(self):
        PromoCode.objects.create(code='PRO15', discount_percent=15)
        self.add(1)
        r = self.client.post('/api/shop/checkout/', {'promo_code': 'NOPE'}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/api/shop/checkout/', {}, format='json')
        self.assertEqual(Decimal(r.data['discount']), 0)

    def test_out_of_stock_is_refused(self):
        self.add(5)
        self.assertEqual(self.client.post('/api/shop/checkout/', {}, format='json').status_code, 409)

    def test_cancel_unpaid_releases_stock_paid_cannot_be_cancelled(self):
        self.add(2)
        num = self.client.post('/api/shop/checkout/', {}, format='json').data['order_number']
        self.assertEqual(self.client.post(f'/api/shop/orders/{num}/cancel/').status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock_quantity, 3)

        CartItem.objects.create(cart=self.cart, product=self.product, quantity=1)
        num = self.client.post('/api/shop/checkout/', {}, format='json').data['order_number']
        LockerCredit.objects.create(member=self.user, balance=Decimal('200'), max_cap=Decimal('300'))
        r = self.checkout(purpose='shop_order', order_number=num, method='locker_credit')
        self.assertEqual(r.data['status'], 'paid')
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('85.00'))   # 200 - 115
        self.assertEqual(self.client.post(f'/api/shop/orders/{num}/cancel/').status_code, 400)

    def test_credit_must_cover_total(self):
        self.add(1)
        num = self.client.post('/api/shop/checkout/', {}, format='json').data['order_number']
        LockerCredit.objects.create(member=self.user, balance=Decimal('10'))
        self.assertEqual(self.checkout(purpose='shop_order', order_number=num, method='locker_credit').status_code, 400)
        self.assertEqual(Order.objects.get(order_number=num).status, 'pending')

    def test_paying_a_cancelled_order_needs_review(self):
        self.add(1)
        num = self.client.post('/api/shop/checkout/', {}, format='json').data['order_number']
        pay = self.checkout(purpose='shop_order', order_number=num)
        self.client.post(f'/api/shop/orders/{num}/cancel/')
        self.assertEqual(self.moyasar_pays(pay.data).data['status'], 'review')


@override_settings(LOCKER_CREDIT_ENABLED=True)   # tests the locker credit feature itself
class TrainerBookingTests(PortalBase):
    def setUp(self):
        super().setUp()
        coach = User.objects.create_user(username='c', email='c@test.local', password='x', first_name='Coach')
        self.trainer = TrainerProfile.objects.create(user=coach, title='Coach', hourly_rate=Decimal('120'))
        self.day = timezone.localdate() + timedelta(days=2)
        TrainerAvailability.objects.create(trainer=self.trainer, day_of_week=self.day.weekday(),
                                           start_time=time(9, 0), end_time=time(12, 0))

    def at(self, hour):
        return timezone.make_aware(datetime.combine(self.day, time(hour, 0)), timezone.get_current_timezone())

    def book(self, hour):
        return self.client.post('/api/catalog/sessions/', {'trainer_id': self.trainer.id,
                                                            'scheduled_at': self.at(hour).isoformat()}, format='json')

    def test_slots_follow_availability_and_bookings(self):
        r = self.client.get(f'/api/catalog/trainers/{self.trainer.id}/slots/', {'date': self.day.isoformat()})
        self.assertEqual([s['label'] for s in r.data['slots']], ['09:00', '10:00', '11:00'])
        self.assertEqual(r.data['price'], '120.00')
        self.book(10)
        r = self.client.get(f'/api/catalog/trainers/{self.trainer.id}/slots/', {'date': self.day.isoformat()})
        self.assertEqual([s['label'] for s in r.data['slots']], ['09:00', '11:00'])

    def test_booking_outside_hours_or_twice_is_refused(self):
        self.assertEqual(self.book(15).status_code, 400)
        self.assertEqual(self.book(9).status_code, 201)
        self.assertEqual(self.book(9).status_code, 409)

    def test_session_scheduled_after_payment_and_refund_on_cancel(self):
        s = self.book(9).data
        self.assertEqual(s['status'], 'pending_payment')
        pay = self.checkout(purpose='trainer_session', session_id=s['id'])
        self.assertEqual(pay.data['amount'], '120.00')
        self.moyasar_pays(pay.data)
        session = TrainerSession.objects.get(id=s['id'])
        self.assertEqual(session.status, 'scheduled')
        self.assertEqual(self.client.post(f'/api/catalog/sessions/{s["id"]}/cancel/').status_code, 200)
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('120.00'))


class CourseAndRenewalTests(PortalBase):
    def setUp(self):
        super().setUp()
        coach = User.objects.create_user(username='c', email='c@test.local', password='x')
        self.trainer = TrainerProfile.objects.create(user=coach, title='Coach')

    def course(self, price, pro=False):
        return Course.objects.create(trainer=self.trainer, title='VBT', description='d', category='Strength',
                                     price=Decimal(price), status='published', is_included_with_pro=pro)

    def test_paid_course_needs_payment(self):
        c = self.course('49')
        r = self.client.post('/api/catalog/enrollments/', {'course_id': c.id}, format='json')
        self.assertEqual(r.status_code, 402)
        self.assertFalse(Enrollment.objects.exists())
        pay = self.checkout(purpose='course', course_id=c.id)
        self.moyasar_pays(pay.data)
        self.assertEqual(Enrollment.objects.get(member=self.user).amount_paid, Decimal('49.00'))

    def test_free_and_included_courses_enrol_directly(self):
        self.assertEqual(self.client.post('/api/catalog/enrollments/', {'course_id': self.course('0').id}).status_code, 201)
        included = self.course('49', pro=True)              # member is on the Pro plan
        listed = {c['id']: c for c in self.client.get('/api/catalog/courses/').data}
        self.assertTrue(listed[included.id]['free_for_me'])
        self.assertEqual(self.client.post('/api/catalog/enrollments/', {'course_id': included.id}).status_code, 201)

    def test_renewal_extends_from_expiry(self):
        old_expiry = self.membership.expiry_date
        pay = self.checkout(purpose='renewal', plan_id=self.plan.id, months=3)
        self.assertEqual(pay.data['amount'], '237.00')
        self.moyasar_pays(pay.data)
        self.membership.refresh_from_db()
        from dateutil.relativedelta import relativedelta
        self.assertEqual(self.membership.expiry_date, old_expiry + relativedelta(months=3))
        self.assertEqual(Transaction.objects.get(member=self.user).transaction_type, 'subscription')


class PassAndCheckInTests(PortalBase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(username='s', email='s@test.local', password='x', role='admin',
                                              is_staff=True)

    def test_public_verify_endpoint_is_gone(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f'/api/members/verify/{self.user.id}/').status_code, 404)

    def test_pass_token_resolves_for_staff_only(self):
        token = self.client.get('/api/members/pass/').data['token']
        self.assertTrue(token.startswith('SGPASS:'))
        self.assertEqual(self.client.get('/api/admin/registrations-api/members/lookup/', {'pass': token}).status_code, 403)
        self.client.force_authenticate(self.staff)
        r = self.client.get('/api/admin/registrations-api/members/lookup/', {'pass': token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['member']['id'], self.user.id)

    def test_expired_or_forged_pass_is_refused(self):
        self.client.force_authenticate(self.staff)
        token = make_token(self.membership)
        with mock.patch('apps.members.passes.MAX_AGE_SECONDS', -1):
            self.assertEqual(self.client.get('/api/admin/registrations-api/members/lookup/', {'pass': token}).status_code, 400)
        self.assertEqual(self.client.get('/api/admin/registrations-api/members/lookup/',
                                         {'pass': 'SGPASS:' + signing.dumps({'m': self.user.id}, salt='other')}).status_code, 400)

    def test_members_cannot_check_themselves_in(self):
        self.assertEqual(self.client.post('/api/attendance/check-ins/', {}).status_code, 405)
        self.assertEqual(self.client.post('/api/members/check-ins/', {}).status_code, 405)
        self.assertFalse(CheckIn.objects.exists())

    def test_staff_check_in_updates_milestones_and_notifies(self):
        Milestone.objects.create(tier='T1', title='First visit', target_days=1)
        self.client.force_authenticate(self.staff)
        r = self.client.post(f'/api/admin/registrations-api/members/{self.user.id}/check-in/', {}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(MemberMilestone.objects.get(member=self.user).current_days, 1)
        self.assertTrue(Notification.objects.filter(recipient=self.user, category='attendance').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.user, title='Reward unlocked').exists())


class AttendanceAndDashboardTests(PortalBase):
    def test_milestones_list_hides_reward_code_until_claimed(self):
        m = Milestone.objects.create(tier='T1', title='Week', target_days=1, reward_code='SECRET-1')
        r = self.client.get('/api/attendance/milestones/')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.data[0]['reward_code'])
        MemberMilestone.objects.create(member=self.user, milestone=m, status='active', current_days=1)
        self.assertEqual(self.client.post(f'/api/attendance/milestones/{m.id}/claim/').data['reward_code'], 'SECRET-1')
        self.assertEqual(self.client.get('/api/attendance/milestones/').data[0]['reward_code'], 'SECRET-1')

    def test_milestone_cannot_be_claimed_early(self):
        m = Milestone.objects.create(tier='T1', title='Week', target_days=7)
        MemberMilestone.objects.create(member=self.user, milestone=m, status='active', current_days=2)
        self.assertEqual(self.client.post(f'/api/attendance/milestones/{m.id}/claim/').status_code, 400)

    def test_summary_has_no_invented_figures(self):
        CheckIn.objects.create(member=self.user)
        data = self.client.get('/api/attendance/summary/').data
        text = str(data)
        for fake in ('Top 4%', '78%', 'Recovery index', 'Bio-Synced', 'Cycle Goal'):
            self.assertNotIn(fake, text)
        self.assertEqual(data['current_streak']['value'], 1)

    def test_dashboard_has_no_fake_vitals_and_links_directives(self):
        data = self.client.get('/api/dashboard/').data
        self.assertIsNone(data['biometric'])                     # no workouts yet → nothing invented
        self.assertNotIn('heart_rate', str(data))
        for d in data['directives']:
            self.assertTrue(d['link'].startswith('/dashboard'))
        occ = self.client.get('/api/dashboard/occupancy/').data
        self.assertEqual(occ['current'], 0)

    def test_occupancy_counts_open_check_ins(self):
        CheckIn.objects.create(member=self.user, zone='Free weights')
        with self.settings(GYM_CAPACITY=10):
            occ = self.client.get('/api/dashboard/occupancy/').data
        self.assertEqual((occ['current'], occ['percent']), (1, 10))
        self.assertEqual(occ['zones'][0]['label'], 'Free weights')

    def test_payment_methods_is_read_only(self):
        self.assertEqual(self.client.post('/api/payments/methods/', {'last4': '4242'}).status_code, 405)
        self.assertIn('accepted', self.client.get('/api/payments/methods/').data)
