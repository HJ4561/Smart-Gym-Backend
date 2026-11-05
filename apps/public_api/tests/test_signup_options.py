"""Saudi mobile numbers, per-facility months and map location on website signups."""
from datetime import date
from unittest.mock import patch

from dateutil.relativedelta import relativedelta
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from apps.core.phone import normalize_saudi_mobile
from apps.members.models import FacilityAddon, MembershipFacility, MembershipPlan, MembershipRegistration as R
from apps.members.services import approve_registration
from apps.public_api.serializers import RegistrationCreateSerializer
from apps.site_content.models import SiteItem
from .faces import one_face

# Test photos have no real face — stub the detector to find one well-placed face.
_faces = one_face()


def setUpModule():
    _faces.start()


def tearDownModule():
    _faces.stop()

def png():
    import io

    from PIL import Image
    from .test_payments import clear_image
    buf = io.BytesIO()
    clear_image().save(buf, 'PNG')
    return buf.getvalue()


class PhoneTests(APITestCase):
    def test_saudi_formats(self):
        for raw in ('0551234567', '551234567', '+966551234567', '00966 55 123 4567', '966-55-123-4567'):
            self.assertEqual(normalize_saudi_mobile(raw), '+966551234567', raw)
        for raw in ('0151234567', '+15551234567', '05512345', '', None, '0551234567890'):
            self.assertIsNone(normalize_saudi_mobile(raw), raw)


class SignupOptionsTests(APITestCase):
    def setUp(self):
        self.plan = MembershipPlan.objects.create(name='pro', display_name='Pro', price_monthly=79, price_annual=790)
        self.pool = SiteItem.objects.create(collection='facilities', data={'title': {'en': 'Pool'}})
        FacilityAddon.objects.create(item=self.pool, monthly_price=50, is_bookable=True)

    def data(self, **extra):
        return {'iqama_number': '2123456789', 'full_name': 'Noor Saleh', 'email': 'noor@t.local', 'phone': '0551234567',
                'age': 30, 'gender': 'female', 'plan': self.plan.id, 'duration_months': 6, 'password': 'Strong-pass-1',
                'photo': SimpleUploadedFile('p.png', png(), content_type='image/png'), **extra}

    def test_phone_must_be_saudi(self):
        ser = RegistrationCreateSerializer(data=self.data(phone='+1 555 010 4832'))
        self.assertFalse(ser.is_valid())
        self.assertIn('phone', ser.errors)
        ser = RegistrationCreateSerializer(data=self.data())
        self.assertTrue(ser.is_valid(), ser.errors)
        self.assertEqual(ser.validated_data['phone'], '+966551234567')

    def test_facility_shorter_than_plan_and_location(self):
        ser = RegistrationCreateSerializer(data=self.data(facilities=f'{self.pool.id}:2', address='Riyadh',
                                                          address_lat=24.7136, address_lng=46.6753))
        self.assertTrue(ser.is_valid(), ser.errors)
        line = ser.validated_data['facilities'][0]
        self.assertEqual((line['months'], line['amount']), (2, '100.00'))
        reg = ser.save()
        self.assertEqual(reg.payment_amount, 79 * 6 + 100)
        self.assertAlmostEqual(reg.address_lat, 24.7136)
        R.objects.filter(id=reg.id).update(status=R.STATUS_PAID, payment_status='paid', moyasar_payment_id='pay_x')
        reg.refresh_from_db()
        with patch('apps.public_api.moyasar.confirm_paid_for_approval'):
            approve_registration(reg)
        f = MembershipFacility.objects.get(item_id=self.pool.id)
        self.assertEqual(f.expires_on, date.today() + relativedelta(months=2))   # not the plan's 6 months

    def test_facility_cannot_outlast_plan(self):
        ser = RegistrationCreateSerializer(data=self.data(facilities=f'{self.pool.id}:7'))
        self.assertFalse(ser.is_valid())
        self.assertIn('facilities', ser.errors)


class ReceiptTests(APITestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.member = get_user_model().objects.create_user(username='rm', email='rm@t.local', password='x', role='member',
                                                           first_name='Rana')

    def test_every_payment_emails_a_bilingual_receipt(self):
        from django.core import mail
        from django.test import override_settings
        from apps.payments.gateway import record_transaction
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', GYM_VAT_NUMBER='300000000000003'):
            with self.captureOnCommitCallbacks(execute=True):
                t = record_transaction(self.member, 'Session with Omar', 'coaching', 150, 'MADA •••• 1', 'pay_s')
            self.assertEqual(len(mail.outbox), 1)
            self.assertIn('SAR 150.00', mail.outbox[0].body)
            self.assertIn('استلمنا دفعتك', mail.outbox[0].body)
            from apps.payments.receipts import token_for
            page = self.client.get(f"/api/payments/receipts/{token_for('t', t.id)}/")
            self.assertEqual(page.status_code, 200)
            html = page.content.decode()
            self.assertIn('RECEIPT', html)
            self.assertIn('إيصال', html)
            self.assertIn('300000000000003', html)       # VAT number + ZATCA QR when configured
            self.assertIn("id='zqr'", html)
            with self.captureOnCommitCallbacks(execute=True):
                record_transaction(self.member, 'Refund', 'refund', -150, 'MADA', 'r1')
            self.assertEqual(len(mail.outbox), 1)          # refunds don't send a payment receipt
        self.assertEqual(self.client.get('/api/payments/receipts/forged-token/').status_code, 404)
