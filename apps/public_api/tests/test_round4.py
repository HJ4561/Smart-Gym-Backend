"""
Fourth round: trusted devices (2-step code once per device for 30 days), the gym-wide 2-step
switches in Admin → Settings, and server-side sorting / filtering on the main admin lists.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts import two_factor
from apps.accounts.models import TrustedDevice, TwoFactor
from apps.catalog.models import Course, TrainerProfile
from apps.members.models import Membership, MembershipPlan
from apps.payments.gateway import record_transaction

User = get_user_model()
PW = 'Strong-Pass-123'


class Base(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password=PW, role='admin', first_name='Ad')
        self.secret = two_factor.new_secret()
        TwoFactor.objects.create(user=self.admin, method='totp', secret=self.secret, enabled=True)

    def login(self, device_token=None, email='a@t.local'):
        body = {'email': email, 'password': PW}
        if device_token:
            body['device_token'] = device_token
        return self.client.post('/api/auth/login/', body, format='json')

    def verify(self, ticket, remember=True):
        return self.client.post('/api/auth/2fa/verify/', {'ticket': ticket, 'code': two_factor.totp_now(self.secret),
                                                          'remember_device': remember}, format='json')


class TrustedDeviceTests(Base):
    def test_code_once_then_trusted_for_30_days(self):
        r = self.verify(self.login().data['ticket'])
        token = r.data['device_token']
        self.assertEqual(r.data['trusted_days'], 30)
        self.assertIn('access', self.login(token).data)                           # same device: no code
        self.assertEqual(self.login().data['two_factor'], 'verify')                # another device: code
        self.assertEqual(self.login('made-up-token').data['two_factor'], 'verify')
        TrustedDevice.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.login(token).data['two_factor'], 'verify')           # after 30 days: code again

    def test_not_remembered_unless_asked(self):
        TwoFactor.objects.filter(user=self.admin).update(last_totp_step=0)
        r = self.verify(self.login().data['ticket'], remember=False)
        self.assertNotIn('device_token', r.data)
        self.assertFalse(TrustedDevice.objects.exists())

    def test_password_or_2fa_changes_forget_devices(self):
        token = self.verify(self.login().data['ticket']).data['device_token']
        self.client.force_authenticate(self.admin)
        status = self.client.get('/api/auth/2fa/').data
        self.assertEqual(len(status['trusted_devices']), 1)
        self.client.post('/api/auth/change-password/', {'old_password': PW, 'new_password': 'Another-Pass-456',
                                                         'new_password_confirm': 'Another-Pass-456'}, format='json')
        self.assertFalse(TrustedDevice.objects.filter(user=self.admin).exists())
        self.client.force_authenticate(None)
        r = self.client.post('/api/auth/login/', {'email': 'a@t.local', 'password': 'Another-Pass-456',
                                                  'device_token': token}, format='json')
        self.assertEqual(r.data['two_factor'], 'verify')

    def test_forget_one_or_all_devices(self):
        TrustedDevice.objects.create(user=self.admin, token_hash='x' * 64, expires_at=timezone.now() + timedelta(days=5))
        TrustedDevice.objects.create(user=self.admin, token_hash='y' * 64, expires_at=timezone.now() + timedelta(days=5))
        self.client.force_authenticate(self.admin)
        one = TrustedDevice.objects.first()
        self.assertEqual(len(self.client.post('/api/auth/2fa/devices/forget/', {'id': one.id}, format='json')
                             .data['trusted_devices']), 1)
        self.assertEqual(self.client.post('/api/auth/2fa/devices/forget/').data['trusted_devices'], [])


class GymWideSwitchTests(Base):
    def switch(self, user, **body):
        self.client.force_authenticate(user)
        r = self.client.patch('/api/admin/settings/security/', {**body}, format='json')
        self.client.force_authenticate(None)
        return r

    def test_off_means_password_only_for_everyone(self):
        self.assertEqual(self.switch(self.admin, two_factor_enabled=False).status_code, 400)     # password needed
        r = self.switch(self.admin, two_factor_enabled=False, password=PW)
        self.assertEqual((r.status_code, r.data['enabled']), (200, False))
        self.assertIn('access', self.login().data)                         # set up, but not asked while off
        self.switch(self.admin, two_factor_enabled=True, password=PW)
        self.assertEqual(self.login().data['two_factor'], 'verify')

    def test_optional_for_staff_and_only_admins_change_it(self):
        mgr = User.objects.create_user(username='m', email='mg@t.local', password=PW, role='manager')
        self.assertEqual(self.login(email='mg@t.local').data['two_factor'], 'setup')      # required by default
        self.assertEqual(self.switch(mgr, two_factor_required_for_staff=False, password=PW).status_code, 403)
        self.switch(self.admin, two_factor_required_for_staff=False, password=PW)
        self.assertIn('access', self.login(email='mg@t.local').data)                     # optional now
        self.client.force_authenticate(self.admin)
        self.assertFalse(self.client.get('/api/admin/settings/').data['security']['required_for_staff'])
        # staff may turn their own 2FA off while it's optional
        self.assertNotIn('Your role must keep', str(self.client.post('/api/auth/2fa/disable/', {'password': PW, 'code': '1'},
                                                                      format='json').data))


class ListingTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password=PW, role='admin')
        self.client.force_authenticate(self.admin)
        plan = MembershipPlan.objects.create(name='pro', display_name='Pro', price_monthly=79, price_annual=790)
        today = timezone.localdate()
        for i, (first, days) in enumerate((('Zaid', 90), ('Amal', 10), ('Lina', 40))):
            u = User.objects.create_user(username=f'u{i}', email=f'u{i}@t.local', password=PW, role='member', first_name=first)
            Membership.objects.create(member=u, plan=plan, start_date=today, expiry_date=today + timedelta(days=days),
                                      iqama_number=f'21111111{i}0')
            record_transaction(u, f'Payment {i}', 'subscription', Decimal(50 + i * 25), 'Card', f'p{i}', email=False)

    def test_members_sort_and_filter(self):
        names = lambda r: [x['full_name'] for x in r.data['results']]  # noqa: E731
        self.assertEqual(names(self.client.get('/api/admin/members/', {'sort': 'name'})), ['Amal', 'Lina', 'Zaid'])
        self.assertEqual(names(self.client.get('/api/admin/members/', {'sort': '-expiry'})), ['Zaid', 'Lina', 'Amal'])
        self.assertEqual(names(self.client.get('/api/admin/members/', {'filter_status': 'expiring'})), ['Amal'])
        before = (timezone.localdate() + timedelta(days=50)).isoformat()
        self.assertEqual(len(self.client.get('/api/admin/members/', {'filter_expires_before': before}).data['results']), 2)
        self.assertEqual(self.client.get('/api/admin/members/', {'sort': 'password'}).status_code, 400)
        self.assertEqual(self.client.get('/api/admin/members/', {'filter_active': 'maybe'}).status_code, 400)

    def test_transactions_sort_and_amount_filter(self):
        r = self.client.get('/api/admin/finance/transactions/', {'sort': '-amount'}).data['results']
        self.assertEqual([x['amount'] for x in r], [100.0, 75.0, 50.0])
        r = self.client.get('/api/admin/finance/transactions/', {'filter_min_amount': '60', 'sort': 'amount'}).data['results']
        self.assertEqual([x['amount'] for x in r], [75.0, 100.0])

    def test_courses_trainers_orders_registrations_accept_the_same_syntax(self):
        coach = TrainerProfile.objects.create(user=User.objects.create_user(username='c', email='c@t.local', role='trainer',
                                                                            first_name='Omar'), title='Coach', rating=4.5)
        TrainerProfile.objects.create(user=User.objects.create_user(username='d', email='d@t.local', role='trainer',
                                                                    first_name='Badr'), title='Coach', rating=3.5)
        Course.objects.create(trainer=coach, title='Cheap', description='x', category='Strength', price=10)
        Course.objects.create(trainer=coach, title='Dear', description='x', category='Strength', price=90)
        self.assertEqual([c['title'] for c in self.client.get('/api/admin/courses/', {'sort': '-price'}).data], ['Dear', 'Cheap'])
        self.assertEqual([c['title'] for c in self.client.get('/api/admin/courses/', {'filter_max_price': '50'}).data], ['Cheap'])
        self.assertEqual([t['first_name'] for t in self.client.get('/api/admin/trainers/', {'sort': '-rating'}).data],
                         ['Omar', 'Badr'])
        self.assertEqual(self.client.get('/api/admin/shop/orders/', {'sort': '-total', 'filter_status': 'paid'}).status_code, 200)
        self.assertEqual(self.client.get('/api/admin/registrations-api/registrations/',
                                         {'sort': '-submitted_at', 'filter_status': 'paid'}).status_code, 200)
