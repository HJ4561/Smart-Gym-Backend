"""
Emails go out in the reader's language (their saved preference, or the language they signed up in);
the payment receipt email stays in both languages with the reader's first.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from apps.accounts import two_factor
from apps.admin_api import members_ops, staff_ops
from apps.members.models import MembershipPlan, MembershipRegistration as R
from apps.members.services import approve_registration, reject_registration
from apps.payments import gateway

from .faces import one_face
from .test_payments import PaymentTestBase

User = get_user_model()
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'
_faces = one_face()


def setUpModule():
    _faces.start()


def tearDownModule():
    _faces.stop()


def html_of(msg):
    return next((body for body, kind in msg.alternatives if kind == 'text/html'), '')


@override_settings(EMAIL_BACKEND=LOCMEM)
class AccountEmailTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='s', email='s@t.local', password='x', role='admin')

    def user(self, lang, role='member'):
        return User.objects.create_user(username=f'{lang}{role}', email=f'{lang}{role}@t.local', password='x',
                                        role=role, first_name='Sara', language=lang)

    def test_code_email_in_the_readers_language(self):
        two_factor._send_email_code(self.user('ar'), '123456', 'login')
        msg = mail.outbox[-1]
        self.assertEqual(msg.subject, 'رمز سمارت جيم: 123456')
        self.assertIn('رمز تسجيل الدخول في سمارت جيم هو', msg.body)
        self.assertNotIn('Your Smart Gym code', msg.body)
        self.assertIn("dir='rtl'", html_of(msg))
        two_factor._send_email_code(self.user('en'), '654321', 'login')
        msg = mail.outbox[-1]
        self.assertEqual(msg.subject, 'Smart Gym code: 654321')
        self.assertNotIn('سمارت جيم', msg.body)
        self.assertEqual(msg.alternatives, [])

    def test_forgotten_password_link(self):
        self.user('ar')
        self.client.post('/api/auth/password/forgot/', {'email': 'armember@t.local'}, content_type='application/json')
        msg = mail.outbox[-1]
        self.assertEqual(msg.subject, 'سمارت جيم — إعادة تعيين كلمة المرور')
        self.assertIn('/reset-password?uid=', msg.body)
        self.assertNotIn('Hi Sara', msg.body)

    def test_temporary_password_and_staff_invite(self):
        self.assertIsNone(members_ops._reset_password(self.user('ar'), self.staff))
        msg = mail.outbox[-1]
        self.assertEqual(msg.subject, 'سمارت جيم — كلمة المرور المؤقتة')
        self.assertIn('كلمة المرور المؤقتة:', msg.body)
        self.assertIsNone(staff_ops._invite(self.user('ar', 'trainer'), self.staff))
        msg = mail.outbox[-1]
        self.assertEqual(msg.subject, 'سمارت جيم — بيانات الدخول إلى بوابة المدربين')
        self.assertIn('بصفة مدرب', msg.body)
        self.assertIn('/coach/login', msg.body)
        self.assertIsNone(staff_ops._invite(self.user('en', 'manager'), self.staff))
        self.assertEqual(mail.outbox[-1].subject, 'Smart Gym — your admin sign-in')

    def test_receipt_email_is_bilingual_with_the_readers_language_first(self):
        member = self.user('ar')
        with self.captureOnCommitCallbacks(execute=True):
            gateway.record_transaction(member, 'Course', 'coaching', Decimal('115.00'), 'mada', 'p')
        msg = mail.outbox[-1]
        self.assertTrue(msg.subject.startswith('تم استلام الدفع · Payment received'))
        self.assertLess(msg.body.index('استلمنا دفعتك'), msg.body.index('We received your payment'))
        member.language = 'en'
        member.save(update_fields=['language'])
        with self.captureOnCommitCallbacks(execute=True):
            gateway.record_transaction(member, 'Course', 'coaching', Decimal('115.00'), 'mada', 'p2')
        msg = mail.outbox[-1]
        self.assertTrue(msg.subject.startswith('Payment received · تم استلام الدفع'))
        self.assertLess(msg.body.index('We received your payment'), msg.body.index('استلمنا دفعتك'))


@override_settings(EMAIL_BACKEND=LOCMEM)
class RegistrationLanguageTests(PaymentTestBase):
    def test_arabic_signup_gets_arabic_emails_and_an_arabic_account(self):
        reg_id = self.register(language='ar').data['id']
        self.assertEqual(self.reg(reg_id).language, 'ar')
        self.pay('pay_ar', reg_id, 23700)
        self.verify('pay_ar')
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            user = approve_registration(self.reg(reg_id))
        self.assertEqual(user.language, 'ar')
        welcome = next(m for m in mail.outbox if 'عضويتك مفعّلة' in m.subject)
        self.assertIn('تمت الموافقة على تسجيلك', welcome.body)
        self.assertNotIn('Your registration is approved', welcome.body)

    def test_accept_language_sets_it_and_english_is_the_default(self):
        r = self.client.post('/api/public/register/', self.payload(), format='multipart', HTTP_ACCEPT_LANGUAGE='ar')
        self.assertEqual(self.reg(r.data['id']).language, 'ar')
        R.objects.all().delete()
        self.assertEqual(self.reg(self.register().data['id']).language, 'en')

    def payload(self):
        from .test_payments import IQAMA, SIGNUP_PASSWORD, photo
        return dict(iqama_number=IQAMA, full_name='Test User', email='Test@Example.com', phone='0500000000', age=30,
                    gender='male', plan=self.plan.id, duration_months=3, password=SIGNUP_PASSWORD, photo=photo())

    def test_rejection_in_the_signup_language(self):
        plan = MembershipPlan.objects.first() or self.plan
        reg = R.objects.create(iqama_number='2999999999', full_name='New Person', email='n@t.local', phone='1', age=30,
                               gender='male', plan=plan, duration_months=1, language='ar')
        reject_registration(reg, 'Iqama photo unclear')
        msg = mail.outbox[-1]
        self.assertEqual(msg.subject, 'تسجيلك في سمارت جيم')
        self.assertIn('السبب: Iqama photo unclear', msg.body)
        self.assertNotIn('We could not approve', msg.body)
