"""
ZATCA B2B: companies a member buys for get standard tax invoices that ZATCA clears before handover.
ZATCA is mocked; the clearance answer carries a stamped copy with ZATCA's own QR, like the real one.
"""
import base64
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.payments import gateway
from apps.payments.models import PaymentIntent
from apps.zatca import client, service, signer
from apps.zatca.models import BusinessCustomer, ZatcaDocument, ZatcaEvent

from .test_zatca import KEY, REPORTED, csr, make_device, self_signed, turn_on, verify

User = get_user_model()
COMPANY = {'name': 'شركة الاختبار المحدودة', 'vat_number': '311111111101113', 'crn': '1010101010',
           'street': 'King Fahd Road', 'building_number': '1234', 'additional_number': '', 'district': 'Al Olaya',
           'city': 'Riyadh', 'postal_code': '12211'}
STAMPED_QR = base64.b64encode(b'\x01\x05ZATCA').decode()


def cleared(*args, **kwargs):
    """ZATCA's clearance answer: the invoice back, with its own QR in place of ours."""
    xml = args[5]
    stamped = xml.split("<cbc:ID>QR</cbc:ID>")[0] + ("<cbc:ID>QR</cbc:ID>\n        <cac:Attachment>\n"
                                                    "            <cbc:EmbeddedDocumentBinaryObject mimeCode=\"text/plain\">"
                                                    f"{STAMPED_QR}</cbc:EmbeddedDocumentBinaryObject>"
                                                    + xml.split("<cbc:ID>QR</cbc:ID>")[1].split('</cbc:EmbeddedDocumentBinaryObject>', 1)[1])
    return client.Result(200, {'clearanceStatus': 'CLEARED', 'clearedInvoice': base64.b64encode(stamped.encode()).decode()})


def turn_on_b2b():
    device = turn_on()
    device.invoice_types = '1100'
    device.save(update_fields=['invoice_types'])
    return device


@override_settings(ZATCA_ENCRYPTION_KEY=KEY, ZATCA_SEND_INLINE=True)
class CompanyInvoiceTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member',
                                               first_name='Sara', last_name='Ali')
        self.device = turn_on_b2b()
        self.company = BusinessCustomer.objects.create(owner=self.member, **COMPANY)

    def pay(self, amount, buyer=None, **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return gateway.record_transaction(self.member, 'Corporate membership', 'subscription', Decimal(amount),
                                              'mada', 'pay_b2b', email=False, buyer=buyer, **kw)

    def test_company_sale_is_cleared_and_prints_zatcas_qr(self):
        with patch.object(client, 'clear', side_effect=cleared) as clear, patch.object(client, 'report') as report:
            txn = self.pay('1150.00', buyer=self.company)
        doc = ZatcaDocument.objects.get(transaction=txn)
        self.assertEqual((doc.status, doc.is_standard, doc.customer_name), ('cleared', True, COMPANY['name']))
        self.assertEqual((clear.call_count, report.call_count), (1, 0))
        self.assertEqual(doc.qr, STAMPED_QR)                               # ZATCA's stamp, not ours
        self.assertIn(STAMPED_QR, doc.cleared_xml)
        for part in ('<cbc:InvoiceTypeCode name="0100000">388</cbc:InvoiceTypeCode>', '<cbc:CompanyID>311111111101113</cbc:CompanyID>',
                     '<cbc:ActualDeliveryDate>', '<cbc:ID schemeID="CRN">1010101010</cbc:ID>'):
            self.assertIn(part, doc.signed_xml)
        self.assertEqual(signer.invoice_hash(doc.signed_xml), doc.invoice_hash)
        self.assertEqual(doc.buyer_details['vat_number'], '311111111101113')

    def test_details_are_frozen_on_the_invoice(self):
        with patch.object(client, 'clear', side_effect=cleared):
            txn = self.pay('100.00', buyer=self.company)
        self.company.name = 'Renamed Co.'
        self.company.save()
        self.assertEqual(ZatcaDocument.objects.get(transaction=txn).buyer_details['name'], COMPANY['name'])

    def test_refund_of_a_company_sale_gets_a_cleared_credit_note_for_the_same_buyer(self):
        with patch.object(client, 'clear', side_effect=cleared):
            sale = self.pay('1150.00', buyer=self.company)
            with self.captureOnCommitCallbacks(execute=True):
                refund = gateway.record_transaction(self.member, 'Refund — Corporate membership', 'refund',
                                                    Decimal('-1150.00'), 'mada', 'refund:b2b', refund_of=sale)
        note = ZatcaDocument.objects.get(transaction=refund)
        self.assertEqual((note.doc_type, note.status, note.is_standard), ('credit', 'cleared', True))
        self.assertEqual(note.buyer_details, ZatcaDocument.objects.get(transaction=sale).buyer_details)
        self.assertIn('<cbc:InvoiceTypeCode name="0100000">381</cbc:InvoiceTypeCode>', note.signed_xml)

    def test_a_simplified_only_device_issues_a_simplified_invoice_and_says_so(self):
        self.device.invoice_types = '0100'
        self.device.save(update_fields=['invoice_types'])
        with patch.object(client, 'report', return_value=REPORTED):
            txn = self.pay('100.00', buyer=self.company)
        doc = ZatcaDocument.objects.get(transaction=txn)
        self.assertEqual((doc.is_standard, doc.status), (False, 'reported'))
        self.assertTrue(ZatcaEvent.objects.filter(action='b2b_unavailable').exists())
        verify(doc)

    def test_zatca_unreachable_says_pending_clearance_until_the_retry_clears_it(self):
        with patch.object(client, 'clear', return_value=client.Result(0, {}, 'ZATCA could not be reached')):
            txn = self.pay('100.00', buyer=self.company)
        doc = ZatcaDocument.objects.get(transaction=txn)
        self.assertEqual(doc.status, 'pending')
        from apps.payments.receipts import from_transaction, render_html
        html = render_html(from_transaction(txn))
        self.assertIn('Pending clearance', html)
        self.assertIn('TAX INVOICE', html)
        self.assertIn('Buyer VAT No.', html)
        self.assertIn('311111111101113', html)
        ZatcaDocument.objects.filter(pk=doc.pk).update(next_attempt_at=None)
        with patch.object(client, 'clear', side_effect=cleared):
            self.assertEqual(service.retry_due()['reported'], 1)
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'cleared')
        self.assertNotIn('Pending clearance', render_html(from_transaction(txn)))

    def test_rejected_clearance_is_shown(self):
        bad = client.Result(400, {'validationResults': {'errorMessages': [{'message': 'BR-KSA-63 buyer address'}]},
                                  'clearanceStatus': 'NOT_CLEARED'})
        with patch.object(client, 'clear', return_value=bad):
            txn = self.pay('100.00', buyer=self.company)
        doc = ZatcaDocument.objects.get(transaction=txn)
        self.assertEqual((doc.status, doc.zatca_errors), ('rejected', ['BR-KSA-63 buyer address']))


@override_settings(ZATCA_ENCRYPTION_KEY=KEY, ZATCA_SEND_INLINE=True)
class CompanyApiTests(APITestCase):
    def setUp(self):
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        self.other = User.objects.create_user(username='o', email='o@t.local', password='x', role='member')
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        self.client.force_authenticate(self.member)

    def test_members_save_companies_only_when_b2b_is_live(self):
        self.assertEqual(self.client.get('/api/zatca/companies/').data, {'b2b': False, 'companies': []})
        self.assertEqual(self.client.post('/api/zatca/companies/', COMPANY, format='json').status_code, 400)
        turn_on_b2b()
        r = self.client.post('/api/zatca/companies/', {**COMPANY, 'vat_number': '123', 'postal_code': '1'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('start and end with 3', r.data['vat_number'])
        self.assertIn('الرمز البريدي', r.data['postal_code'])
        r = self.client.post('/api/zatca/companies/', COMPANY, format='json')
        self.assertEqual(r.status_code, 201)
        data = self.client.get('/api/zatca/companies/').data
        self.assertEqual((data['b2b'], [c['name'] for c in data['companies']]), (True, [COMPANY['name']]))

    def test_checkout_takes_only_the_members_own_company(self):
        turn_on_b2b()
        mine = BusinessCustomer.objects.create(owner=self.member, **COMPANY)
        theirs = BusinessCustomer.objects.create(owner=self.other, **COMPANY)
        with override_settings(MOYASAR_PUBLISHABLE_KEY='pk_test_x'):
            r = self.client.post('/api/payments/checkout/', {'purpose': 'locker_topup', 'amount': 50,
                                                             'business_customer_id': theirs.id}, format='json')
            self.assertEqual(r.status_code, 400)
            r = self.client.post('/api/payments/checkout/', {'purpose': 'locker_topup', 'amount': 50,
                                                             'business_customer_id': mine.id}, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(PaymentIntent.objects.get(pk=r.data['id']).business_customer_id, mine.id)

    def test_member_removes_a_company_and_old_invoices_keep_it(self):
        turn_on_b2b()
        c = BusinessCustomer.objects.create(owner=self.member, **COMPANY)
        self.assertEqual(self.client.delete(f'/api/zatca/companies/{c.id}/').status_code, 204)
        self.assertFalse(BusinessCustomer.objects.get(pk=c.id).is_active)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.patch(f'/api/zatca/companies/{c.id}/', {'city': 'Jeddah'}, format='json').status_code, 404)

    def test_admin_lists_and_adds_companies_for_a_member(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post('/api/admin/zatca/companies/', COMPANY, format='json').status_code, 400)
        r = self.client.post('/api/admin/zatca/companies/', {**COMPANY, 'owner_id': self.member.id}, format='json')
        self.assertEqual((r.status_code, r.data['owner_id']), (201, self.member.id))
        rows = self.client.get('/api/admin/zatca/companies/', {'member': self.member.id}).data
        self.assertEqual(len(rows), 1)

    def test_wizard_registers_a_b2b_device_with_six_checks(self):
        self.client.force_authenticate(self.admin)
        key = csr.new_private_key()
        token = base64.b64encode(self_signed(key).encode()).decode()
        issued = client.Result(200, {'binarySecurityToken': token, 'secret': 's', 'requestID': 9})
        r = self.client.post('/api/admin/zatca/onboard/start/', {'environment': 'sandbox', 'b2b': True}, format='json')
        dev = r.data['onboarding']['id']
        self.assertEqual(r.data['onboarding']['invoice_types'], '1100')
        with patch.object(client, 'compliance', return_value=issued):
            self.client.post(f'/api/admin/zatca/onboard/{dev}/otp/', {'otp': '123345'}, format='json')
        with patch.object(client, 'compliance_check', return_value=REPORTED) as check:
            r = self.client.post(f'/api/admin/zatca/onboard/{dev}/checks/', format='json')
        self.assertEqual(check.call_count, 6)
        self.assertTrue(r.data['onboarding']['checks_passed'])
        self.assertIn('standard invoice', r.data['onboarding']['compliance_checks'])
        with patch.object(client, 'production_csid', return_value=issued):
            self.client.post(f'/api/admin/zatca/onboard/{dev}/production/', format='json')
        r = self.client.post(f'/api/admin/zatca/onboard/{dev}/activate/', format='json')
        self.assertEqual((r.data['device']['b2b'], r.data['b2b']), (True, True))

    def test_status_says_b2b_is_off_on_a_simplified_device(self):
        make_device()
        self.client.force_authenticate(self.admin)
        self.assertFalse(self.client.get('/api/admin/zatca/status/').data['b2b'])
