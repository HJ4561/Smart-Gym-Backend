"""Referral links: a member's code, signing up with it, and SAR 50 credit when the gym approves the signup."""
from decimal import Decimal

from django.test import override_settings

from apps.members.models import Membership, MembershipRegistration as R, Referral
from apps.members.services import approve_registration
from apps.notifications.models import Notification
from apps.payments.models import LockerCredit, Transaction

from .faces import one_face
from .test_payments import IQAMA, PaymentTestBase

_faces = one_face()


def setUpModule():
    _faces.start()


def tearDownModule():
    _faces.stop()


@override_settings(PUBLIC_SITE_URL='https://site.example')
class ReferralTests(PaymentTestBase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        self.sara = get_user_model().objects.create_user(username='sara', email='sara@t.local', password='x',
                                                         role='member', first_name='Sara', phone='0511111111')
        Membership.objects.create(member=self.sara, plan=self.plan, status='active', start_date='2026-01-01',
                                  expiry_date='2027-01-01', iqama_number='2111111111')

    def my_referral(self, user):
        self.client.force_authenticate(user)
        data = self.client.get('/api/members/referral/').data
        self.client.force_authenticate(None)
        return data

    def signup_and_approve(self, code, pid='pay_ref', **extra):
        r = self.register(referral_code=code, **extra)
        self.assertEqual(r.status_code, 201, r.data)
        self.pay(pid, r.data['id'], 23700)
        self.verify(pid)
        with self.captureOnCommitCallbacks(execute=True):
            return approve_registration(self.reg(r.data['id']))

    def test_member_gets_a_code_and_link(self):
        data = self.my_referral(self.sara)
        self.assertEqual(len(data['code']), 8)
        self.assertEqual(data['link'], f"https://site.example/join?ref={data['code']}")
        self.assertEqual((data['reward'], data['joined'], data['earned']), ('50.00', 0, '0.00'))
        self.assertEqual(self.my_referral(self.sara)['code'], data['code'])      # stays the same

    def test_public_lookup(self):
        code = self.my_referral(self.sara)['code']
        r = self.client.get(f'/api/public/referral/?code={code.lower()}')
        self.assertEqual((r.status_code, r.data['referrer_first_name']), (200, 'Sara'))
        self.assertEqual(self.client.get('/api/public/referral/?code=NOPE1234').status_code, 404)

    def test_approved_signup_pays_the_referrer_once(self):
        code = self.my_referral(self.sara)['code']
        new = self.signup_and_approve(code)
        self.assertEqual(LockerCredit.objects.get(member=self.sara).balance, Decimal('50.00'))
        ref = Referral.objects.get(referred=new)
        self.assertEqual((ref.referrer, ref.reward), (self.sara, Decimal('50.00')))
        line = Transaction.objects.get(member=self.sara)
        self.assertEqual((line.payment_method_label, line.amount), ('Locker credit', Decimal('50.00')))
        self.assertTrue(Notification.objects.filter(recipient=self.sara, title__startswith='Referral reward').exists())
        data = self.my_referral(self.sara)
        self.assertEqual((data['joined'], data['earned']), (1, '50.00'))
        # Not counted as money received.
        from apps.admin_api.views import cash_transactions
        self.assertFalse(cash_transactions(Transaction.objects.filter(member=self.sara)).exists())

    def test_invalid_code_is_refused_at_signup(self):
        r = self.register(referral_code='WRONG123')
        self.assertEqual(r.status_code, 400)
        self.assertIn('referral_code', r.data)
        self.assertFalse(R.objects.exists())

    def test_no_reward_for_referring_yourself_or_before_approval(self):
        code = self.my_referral(self.sara)['code']
        r = self.register(referral_code=code)
        self.assertEqual(self.reg(r.data['id']).referral_code, code)
        self.assertFalse(LockerCredit.objects.filter(member=self.sara).exists())     # nothing until approved
        R.objects.all().delete()
        Membership.objects.filter(member=self.sara).update(iqama_number=IQAMA)       # same Iqama = same person
        self.signup_and_approve(code, pid='pay_self')
        self.assertFalse(Referral.objects.exists())
        self.assertFalse(LockerCredit.objects.filter(member=self.sara, balance__gt=0).exists())

    def test_staff_have_no_referral_link(self):
        from django.contrib.auth import get_user_model
        coach = get_user_model().objects.create_user(username='c', email='c@t.local', password='x', role='trainer')
        self.client.force_authenticate(coach)
        self.assertEqual(self.client.get('/api/members/referral/').status_code, 403)
