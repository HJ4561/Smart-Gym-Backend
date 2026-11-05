"""
ZATCA e-invoicing: the VAT engine, signing and QR, the invoice chain, the payment hooks, reporting
and retries, receipts, and the admin API. ZATCA itself is never called — `client` is mocked and the
device signs with a locally made certificate, so every signature can be verified here.
"""
import base64
import datetime
from decimal import Decimal
from unittest.mock import patch

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.members.models import MembershipPlan, MembershipRegistration
from apps.payments import gateway
from apps.payments.models import Transaction
from apps.zatca import client, crypto, csr, service, signer, vat, xml_builder as X
from apps.zatca.models import ZatcaConfig, ZatcaDevice, ZatcaDocument

User = get_user_model()
KEY = Fernet.generate_key().decode()
REPORTED = client.Result(200, {'reportingStatus': 'REPORTED', 'validationResults': {'status': 'PASS'}})


def self_signed(key):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Test eInvoicing')])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(4242).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


def make_device(environment='sandbox', status='active'):
    key = csr.new_private_key()
    body = self_signed(key)
    token = base64.b64encode(body.encode()).decode()
    return ZatcaDevice.objects.create(
        environment=environment, status=status, common_name='SmartGym-EGS-T', serial='1-SmartGym|2-Web|3-x',
        private_key_enc=crypto.encrypt(csr.private_key_pem(key)), production_token_enc=crypto.encrypt(token),
        production_secret_enc=crypto.encrypt('secret'), certificate=body,
        certificate_expires_at=timezone.now() + datetime.timedelta(days=365), activated_at=timezone.now())


def turn_on(environment='sandbox'):
    cfg = ZatcaConfig.load()
    cfg.environment, cfg.enabled, cfg.enabled_at = environment, True, timezone.now() - datetime.timedelta(minutes=1)
    cfg.save()
    return make_device(environment)


def verify(doc):
    """Recompute the hash from the stored XML and check the signature against the certificate."""
    assert signer.invoice_hash(doc.signed_xml) == doc.invoice_hash
    cert = signer.Certificate.from_body(doc.device.certificate)
    qr = signer.read_qr(doc.qr)
    cert.cert.public_key().verify(base64.b64decode(qr[7]), base64.b64decode(doc.invoice_hash), ec.ECDSA(hashes.SHA256()))
    return qr


class VatEngineTests(TestCase):
    def test_uneven_quantity_is_split_so_every_line_multiplies_exactly(self):
        b = vat.split([('Shake', 3, '30.00')])   # 3 × 10.00
        self.assertEqual([(l.quantity, vat.to_sar(l.unit_net)) for l in b.lines],
                         [(2, Decimal('8.70')), (1, Decimal('8.69'))])
        self.assertEqual(vat.to_sar(b.net), Decimal('26.09'))
        self.assertEqual(vat.to_sar(b.paid), Decimal('30.00'))
        self.assertEqual(b.net + b.vat + b.rounding, b.paid)

    def test_total_always_equals_what_was_paid(self):
        for amounts in (['9.00'], ['300.00', '9.00', '0.05'], ['79.00', '120.00'], ['0.01'], ['1234.56']):
            b = vat.split([(f'x{i}', 1, a) for i, a in enumerate(amounts)])
            self.assertEqual(b.paid, sum(vat.to_halalas(a) for a in amounts))
            self.assertEqual(b.inclusive + b.rounding, b.paid)
            self.assertLessEqual(abs(b.rounding), len(amounts))
            for line in b.lines:
                self.assertEqual(line.net, line.quantity * line.unit_net)
                self.assertEqual(line.vat, (line.net * 1500 * 2 + 10_000) // 20_000)


@override_settings(ZATCA_ENCRYPTION_KEY=KEY)
class SignerTests(TestCase):
    def test_signed_invoice_verifies_and_qr_carries_amount_due(self):
        key = csr.new_private_key()
        cert = signer.Certificate.from_body(self_signed(key))
        now = timezone.localtime().replace(microsecond=0)
        b = vat.split([('Membership', 1, '300.00'), ('Shake', 3, '10.00')])
        seller = X.Seller('سمارت جيم', '399999999900003', '1010010000', 'Prince Sultan', '2322', 'Al-Murabba', 'Riyadh', '23333')
        doc = X.Document('invoice', 'T-1', '0f0c5b1e-0000-4000-8000-000000000001', now, 1, X.FIRST_PIH, seller, b)
        s = signer.sign(X.build(doc), private_key=key, certificate=cert, seller_name=seller.name,
                        vat_number=seller.vat_number, timestamp=now.strftime('%Y-%m-%dT%H:%M:%S'),
                        total=f'{vat.to_sar(b.paid):.2f}', vat=f'{vat.to_sar(b.vat):.2f}', signing_time='2026-11-03T10:00:00')
        self.assertEqual(signer.invoice_hash(s.xml), s.invoice_hash)      # the hash ignores the parts added after
        key.public_key().verify(base64.b64decode(s.signature), base64.b64decode(s.invoice_hash), ec.ECDSA(hashes.SHA256()))
        qr = signer.read_qr(s.qr)
        self.assertEqual((qr[1], qr[2], qr[4], qr[5]), ('سمارت جيم', '399999999900003', '310.00', '40.44'))
        self.assertEqual(qr[6], s.invoice_hash)
        self.assertIn('<cbc:PayableRoundingAmount currencyID="SAR">-0.01</cbc:PayableRoundingAmount>', s.xml)
        self.assertNotIn('<!--', s.xml)

    def test_credit_note_needs_reference_and_reason(self):
        b = vat.split([('x', 1, '10')])
        seller = X.Seller('n', '399999999900003', '1010010000', 's', '1234', 'd', 'c', '12345')
        with self.assertRaises(ValueError):
            X.build(X.Document('credit', 'C-1', 'u', timezone.localtime(), 2, X.FIRST_PIH, seller, b))

    def test_private_key_is_stored_encrypted(self):
        device = make_device()
        self.assertNotIn('PRIVATE KEY', device.private_key_enc)
        self.assertIn('PRIVATE KEY', crypto.decrypt(device.private_key_enc))


@override_settings(ZATCA_ENCRYPTION_KEY=KEY, ZATCA_SEND_INLINE=True)
class PaymentHookTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member',
                                               first_name='Sara', last_name='Ali')
        self.device = turn_on()

    def pay(self, amount, desc='Course: Strength 101', tx_type='coaching', **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return gateway.record_transaction(self.member, desc, tx_type, Decimal(amount), 'mada', 'pay_x', **kw)

    @patch.object(client, 'report', return_value=REPORTED)
    def test_paid_sale_is_invoiced_signed_reported_and_chained(self, report):
        t1, t2 = self.pay('120.00'), self.pay('79.00', 'Membership renewal', 'subscription')
        d1, d2 = ZatcaDocument.objects.get(transaction=t1), ZatcaDocument.objects.get(transaction=t2)
        self.assertEqual((d1.status, d2.status), ('reported', 'reported'))
        self.assertEqual((d1.icv, d2.icv), (1, 2))
        self.assertEqual(d1.pih, X.FIRST_PIH)
        self.assertEqual(d2.pih, d1.invoice_hash)                       # the chain
        self.assertEqual((d1.total, d1.net, d1.vat), (Decimal('120.00'), Decimal('104.35'), Decimal('15.65')))
        qr = verify(d1)
        self.assertEqual(qr[4], '120.00')
        self.assertEqual(report.call_count, 2)
        self.device.refresh_from_db()
        self.assertEqual((self.device.last_icv, self.device.last_hash), (2, d2.invoice_hash))

    @patch.object(client, 'report', return_value=REPORTED)
    def test_topups_are_not_invoiced_and_a_sale_is_invoiced_once(self, report):
        self.pay('200.00', 'Locker top-up', 'topup')
        t = self.pay('50.00')
        service.on_sale(t.id)                                            # a second call changes nothing
        self.assertEqual(ZatcaDocument.objects.count(), 1)

    @patch.object(client, 'report', return_value=REPORTED)
    def test_refund_of_an_invoiced_sale_gets_a_linked_credit_note(self, report):
        sale = self.pay('120.00')
        with self.captureOnCommitCallbacks(execute=True):
            refund = gateway.record_transaction(self.member, 'Refund — Course: Strength 101', 'refund',
                                                Decimal('-120.00'), 'Locker credit', 'refund:1', refund_of=sale)
        note = ZatcaDocument.objects.get(transaction=refund)
        self.assertEqual((note.doc_type, note.original.transaction_id, note.total), ('credit', sale.id, Decimal('120.00')))
        self.assertIn('<cbc:InvoiceTypeCode name="0200000">381</cbc:InvoiceTypeCode>', note.signed_xml)
        self.assertIn(f'<cbc:ID>{note.original.number}</cbc:ID>', note.signed_xml)
        verify(note)

    @patch.object(client, 'report', return_value=REPORTED)
    def test_nothing_is_issued_while_zatca_is_off_or_for_older_payments(self, report):
        cfg = ZatcaConfig.load()
        cfg.enabled = False
        cfg.save()
        self.pay('120.00')
        self.assertFalse(ZatcaDocument.objects.exists())
        cfg.enabled, cfg.enabled_at = True, timezone.now()
        cfg.save()
        old = Transaction.objects.first()
        service.on_sale(old.id)                                          # paid before the switch was on
        self.assertFalse(ZatcaDocument.objects.exists())

    def test_zatca_unreachable_waits_and_the_retry_job_sends_it(self):
        with patch.object(client, 'report', return_value=client.Result(0, {}, 'ZATCA could not be reached')):
            t = self.pay('120.00')
        doc = ZatcaDocument.objects.get(transaction=t)
        self.assertEqual((doc.status, doc.attempts), ('pending', 1))
        self.assertGreater(doc.next_attempt_at, timezone.now())
        self.assertEqual(service.retry_due()['sent'], 0)                # not due yet
        ZatcaDocument.objects.filter(pk=doc.pk).update(next_attempt_at=timezone.now())
        with patch.object(client, 'report', return_value=REPORTED):
            self.assertEqual(service.retry_due(), {'sent': 1, 'reported': 1, 'waiting': 0, 'rejected': 0})
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'reported')

    def test_rejected_document_stays_in_the_chain(self):
        bad = client.Result(400, {'validationResults': {'errorMessages': [{'message': 'BR-KSA-XX failed'}]}})
        with patch.object(client, 'report', return_value=bad):
            t1 = self.pay('10.00')
        with patch.object(client, 'report', return_value=REPORTED):
            t2 = self.pay('20.00')
        d1, d2 = ZatcaDocument.objects.get(transaction=t1), ZatcaDocument.objects.get(transaction=t2)
        self.assertEqual((d1.status, d1.zatca_errors), ('rejected', ['BR-KSA-XX failed']))
        self.assertEqual((d2.icv, d2.pih), (2, d1.invoice_hash))

    def test_a_zatca_failure_never_breaks_the_payment(self):
        with patch.object(service, '_issue', side_effect=RuntimeError('boom')):
            t = self.pay('120.00')
        self.assertTrue(Transaction.objects.filter(pk=t.pk).exists())
        self.assertFalse(ZatcaDocument.objects.exists())
        with patch.object(service, 'config', side_effect=RuntimeError('no such table')):
            t = self.pay('99.00')                                       # e.g. not migrated yet
        self.assertTrue(Transaction.objects.filter(pk=t.pk).exists())

    @patch.object(client, 'report', return_value=REPORTED)
    def test_shop_order_lines_share_what_was_paid(self, report):
        from apps.payments.models import PaymentIntent
        from apps.shop.models import Order, OrderItem
        order = Order.objects.create(member=self.member, order_number='ORD-T1', subtotal=Decimal('50.00'),
                                     discount=Decimal('5.00'), tax=Decimal('6.75'), total=Decimal('51.75'))
        OrderItem.objects.create(order=order, product_name='Whey', product_sku='W1', unit_price=Decimal('15.00'),
                                 quantity=2, line_total=Decimal('30.00'))
        OrderItem.objects.create(order=order, product_name='Gloves', product_sku='G1', unit_price=Decimal('20.00'),
                                 quantity=1, line_total=Decimal('20.00'))
        with self.captureOnCommitCallbacks(execute=True):
            txn = gateway.record_transaction(self.member, 'Shop order ORD-T1', 'retail', Decimal('51.75'), 'mada', 'p1')
            PaymentIntent.objects.create(member=self.member, purpose='shop_order', amount=Decimal('51.75'),
                                         description='Shop order', order=order, attempt_token='t', status='paid',
                                         transaction=txn)
        doc = ZatcaDocument.objects.get(transaction=txn)
        self.assertEqual([l['name'] for l in doc.lines], ['Whey', 'Gloves'])
        self.assertEqual(doc.total, Decimal('51.75'))
        verify(doc)


@override_settings(ZATCA_ENCRYPTION_KEY=KEY, ZATCA_SEND_INLINE=True)
class SignupTests(TestCase):
    def setUp(self):
        self.plan = MembershipPlan.objects.create(name='pro', display_name='Pro', price_monthly=79, price_annual=790)
        self.reg = MembershipRegistration.objects.create(
            iqama_number='2999999999', full_name='New Person', email='n@t.local', phone='1', age=30, gender='male',
            plan=self.plan, duration_months=1, status=MembershipRegistration.STATUS_PAID, payment_status='paid',
            payment_amount=Decimal('129.00'), payment_paid_at=timezone.now(), moyasar_payment_id='pay_signup',
            facilities=[{'name': 'Sauna', 'months': 1, 'amount': '50.00'}])
        turn_on()

    @patch.object(client, 'report', return_value=REPORTED)
    def test_signup_is_invoiced_at_payment_and_approval_reuses_that_invoice(self, report):
        doc = service.on_signup_paid(self.reg.id)
        self.assertEqual([(l['name'], l['net']) for l in doc.lines],
                         [('Pro membership × 1 months', '68.70'), ('Sauna × 1 months', '43.48')])
        user = User.objects.create_user(username='n', email='n@t.local', password='x', role='member')
        from apps.members.services import record_signup_payment
        with self.captureOnCommitCallbacks(execute=True):
            txn = record_signup_payment(self.reg, user)
        self.assertEqual(ZatcaDocument.objects.count(), 1)               # no second invoice
        doc.refresh_from_db()
        self.assertEqual(doc.transaction_id, txn.id)

    @patch.object(client, 'report', return_value=REPORTED)
    def test_refunded_signup_gets_a_credit_note(self, report):
        service.on_signup_paid(self.reg.id)
        note = service.on_signup_refund(self.reg.id, 'Iqama not valid')
        self.assertEqual((note.doc_type, note.total, note.reason), ('credit', Decimal('129.00'), 'Iqama not valid'))


@override_settings(ZATCA_ENCRYPTION_KEY=KEY, ZATCA_SEND_INLINE=True)
class ReceiptTests(TestCase):
    @patch.object(client, 'report', return_value=REPORTED)
    def test_receipt_prints_the_simplified_tax_invoice(self, report):
        turn_on()
        member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        with self.captureOnCommitCallbacks(execute=True):
            txn = gateway.record_transaction(member, 'Course', 'coaching', Decimal('115.00'), 'mada', 'p')
        from apps.payments.receipts import from_transaction, render_html
        html = render_html(from_transaction(txn))
        doc = ZatcaDocument.objects.get(transaction=txn)
        for text in ('SIMPLIFIED TAX INVOICE', 'فاتورة ضريبية مبسطة', doc.number, '100.00', '15.00', '115.00',
                     'TEST / ', 'تجريبي', 'Not a legally valid invoice', 'ليست فاتورة ضريبية', 'Amounts in SAR',
                     'End of invoice', 'نهاية الفاتورة', '399999999900003', 'Order type', 'نوع الطلب',
                     'Price (excl. VAT)', 'Total incl. VAT', "id='zqr'"):
            self.assertIn(text, html)
        # Item names in both languages: the Arabic line names the kind of payment.
        self.assertIn("<div dir='rtl'>تدريب</div><div dir='ltr'>Course</div>", html)

    def test_receipt_without_zatca_is_a_plain_receipt(self):
        member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        txn = gateway.record_transaction(member, 'Course', 'coaching', Decimal('115.00'), 'mada', 'p', email=False)
        from apps.payments.receipts import from_transaction, render_html
        html = render_html(from_transaction(txn))
        self.assertIn('RECEIPT', html)
        self.assertIn('إيصال', html)
        self.assertNotIn('TEST / ', html)
        self.assertIn('سمارت جيم', html)            # the gym's Arabic name next to the English



@override_settings(ZATCA_ENCRYPTION_KEY=KEY, ZATCA_SEND_INLINE=True, ZATCA_CRON_SECRET='cron-secret-123')
class AdminApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        self.manager = User.objects.create_user(username='g', email='g@t.local', password='x', role='manager')
        self.client.force_authenticate(self.admin)

    @patch.object(client, 'report', return_value=REPORTED)
    def test_receipt_logo_and_arabic_address(self, report):
        import io
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        png = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(png, 'PNG')
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            bad = self.client.post('/api/admin/zatca/logo/', {'logo': SimpleUploadedFile('x.png', b'no', 'image/png')})
            self.assertEqual(bad.status_code, 400)
            r = self.client.post('/api/admin/zatca/logo/', {'logo': SimpleUploadedFile('logo.png', png.getvalue(), 'image/png')})
            self.assertEqual(r.status_code, 200)
            self.assertIn('zatca/logo', r.data['logo_url'])
            r = self.client.patch('/api/admin/zatca/settings/', {'address_ar': 'شارع الأمير سلطان، الرياض'}, format='json')
            self.assertEqual(r.data['settings']['address_ar'], 'شارع الأمير سلطان، الرياض')
            turn_on()
            member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
            with self.captureOnCommitCallbacks(execute=True):
                txn = gateway.record_transaction(member, 'Course', 'coaching', Decimal('115.00'), 'mada', 'p')
            from apps.payments.receipts import from_transaction, render_html
            html = render_html(from_transaction(txn))
            self.assertIn('شارع الأمير سلطان، الرياض', html)
            self.assertIn("class='logo'", html)
            self.client.force_authenticate(self.manager)
            self.assertEqual(self.client.delete('/api/admin/zatca/logo/').status_code, 403)
            self.client.force_authenticate(self.admin)
            self.assertEqual(self.client.delete('/api/admin/zatca/logo/').data['logo_url'], '')

    def test_status_never_shows_secrets_and_managers_can_only_look(self):
        make_device()
        data = self.client.get('/api/admin/zatca/status/').data
        self.assertEqual((data['device']['private_key'], data['device']['production_certificate']), ('set', 'set'))
        self.assertNotIn('secret', str(data).replace('cron_secret_set', ''))
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.client.get('/api/admin/zatca/status/').status_code, 200)
        self.assertEqual(self.client.patch('/api/admin/zatca/settings/', {'city': 'Jeddah'}, format='json').status_code, 403)

    def test_settings_are_checked_in_english_and_arabic(self):
        r = self.client.patch('/api/admin/zatca/settings/', {'vat_number': '123', 'postal_code': '12'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('start and end with 3', r.data['vat_number'])
        self.assertIn('الرمز البريدي', r.data['postal_code'])
        r = self.client.patch('/api/admin/zatca/settings/', {'enabled': True}, format='json')
        self.assertIn('go-live wizard', r.data['enabled'])

    def test_vat_number_is_locked_while_a_simulation_device_is_live(self):
        cfg = ZatcaConfig.load()
        cfg.environment, cfg.vat_number = 'simulation', '300000000000003'
        cfg.save()
        make_device('simulation')
        r = self.client.patch('/api/admin/zatca/settings/', {'vat_number': '311111111111113'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Locked', r.data['vat_number'])
        self.assertEqual(self.client.patch('/api/admin/zatca/settings/', {'city': 'Jeddah'}, format='json').status_code, 200)

    def test_go_live_wizard_runs_every_step(self):
        key = csr.new_private_key()
        token = base64.b64encode(self_signed(key).encode()).decode()
        issued = client.Result(200, {'binarySecurityToken': token, 'secret': 's3', 'requestID': 77,
                                     'dispositionMessage': 'ISSUED'})
        r = self.client.post('/api/admin/zatca/onboard/start/', {'environment': 'sandbox'}, format='json')
        self.assertEqual(r.status_code, 201)
        dev = r.data['onboarding']['id']
        self.assertIn('BEGIN CERTIFICATE REQUEST', ZatcaDevice.objects.get(pk=dev).csr)
        self.assertEqual(self.client.post(f'/api/admin/zatca/onboard/{dev}/otp/', {'otp': '12'}, format='json').status_code, 400)
        with patch.object(client, 'compliance', return_value=issued):
            self.assertEqual(self.client.post(f'/api/admin/zatca/onboard/{dev}/otp/', {'otp': '123345'}, format='json').status_code, 200)
        with patch.object(client, 'compliance_check', return_value=REPORTED) as check:
            r = self.client.post(f'/api/admin/zatca/onboard/{dev}/checks/', format='json')
        self.assertTrue(r.data['onboarding']['checks_passed'])
        self.assertEqual(check.call_count, 3)
        with patch.object(client, 'production_csid', return_value=issued):
            self.client.post(f'/api/admin/zatca/onboard/{dev}/production/', format='json')
        r = self.client.post(f'/api/admin/zatca/onboard/{dev}/activate/', format='json')
        self.assertEqual((r.data['device']['status'], r.data['enabled'], r.data['live']), ('active', True, True))

    def test_production_needs_confirmation(self):
        r = self.client.post('/api/admin/zatca/onboard/start/', {'environment': 'production'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('legally real', r.data['detail'])

    def test_cron_needs_the_secret(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post('/api/admin/zatca/cron/').status_code, 403)
        r = self.client.post('/api/admin/zatca/cron/', HTTP_X_CRON_SECRET='cron-secret-123')
        self.assertEqual((r.status_code, r.data['sent']), (200, 0))
        self.assertEqual(self.client.get('/api/admin/zatca/cron/', HTTP_AUTHORIZATION='Bearer cron-secret-123').status_code, 200)

    @patch.object(client, 'report', return_value=REPORTED)
    def test_invoice_log_and_xml_download(self, report):
        turn_on()
        member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member', first_name='Sara')
        with self.captureOnCommitCallbacks(execute=True):
            gateway.record_transaction(member, 'Course', 'coaching', Decimal('50.00'), 'mada', 'p', email=False)
        rows = self.client.get('/api/admin/zatca/documents/', {'q': 'Sara'}).data['results']
        self.assertEqual((len(rows), rows[0]['status'], rows[0]['test']), (1, 'reported', True))
        xml = self.client.get(f"/api/admin/zatca/documents/{rows[0]['id']}/xml/")
        self.assertIn(b'<cbc:ProfileID>reporting:1.0</cbc:ProfileID>', xml.content)
