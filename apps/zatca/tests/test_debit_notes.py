"""Debit notes: correcting an undercharge on an issued invoice (signed, linked, reported or cleared)."""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.payments import gateway
from apps.payments.models import Transaction
from apps.payments.receipts import read_token
from apps.zatca import client, signer
from apps.zatca.models import BusinessCustomer, ZatcaDocument

from .test_b2b import COMPANY, cleared, turn_on_b2b
from .test_zatca import KEY, REPORTED, verify

User = get_user_model()


@override_settings(ZATCA_ENCRYPTION_KEY=KEY, ZATCA_SEND_INLINE=True)
class DebitNoteTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        self.manager = User.objects.create_user(username='g', email='g@t.local', password='x', role='manager')
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member',
                                               first_name='Sara')
        self.device = turn_on_b2b()
        self.client.force_authenticate(self.admin)

    def sale(self, amount='115.00', buyer=None):
        with patch.object(client, 'report', return_value=REPORTED), patch.object(client, 'clear', side_effect=cleared), \
                self.captureOnCommitCallbacks(execute=True):
            txn = gateway.record_transaction(self.member, 'Personal training', 'coaching', Decimal(amount), 'mada', 'p1',
                                             email=False, buyer=buyer)
        return ZatcaDocument.objects.get(transaction=txn, doc_type='invoice')

    def debit(self, doc, **body):
        with patch.object(client, 'report', return_value=REPORTED) as report, \
                patch.object(client, 'clear', side_effect=cleared) as clear, self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(f'/api/admin/zatca/documents/{doc.id}/debit/', body, format='json')
        return r, report, clear

    def test_debit_note_is_signed_linked_and_reported(self):
        original = self.sale()
        r, report, _ = self.debit(original, amount='23.00', reason='Session ran 30 minutes over')
        self.assertEqual(r.status_code, 201, r.data)
        note = ZatcaDocument.objects.get(pk=r.data['id'])
        self.assertEqual((note.doc_type, note.original, note.status, note.total, note.vat),
                         ('debit', original, 'reported', Decimal('23.00'), Decimal('3.00')))
        self.assertTrue(note.number.endswith(f'-DBN-{note.icv:06d}'))
        self.assertEqual(note.icv, original.icv + 1)
        self.assertEqual(note.pih, original.invoice_hash)                  # the chain continues
        self.assertIn('<cbc:InvoiceTypeCode name="0200000">383</cbc:InvoiceTypeCode>', note.signed_xml)
        self.assertIn(f'<cbc:ID>{original.number}</cbc:ID>', note.signed_xml)    # billing reference
        self.assertIn('Session ran 30 minutes over', note.signed_xml)
        verify(note)
        self.assertEqual(report.call_count, 1)
        self.assertFalse(Transaction.objects.filter(transaction_id__isnull=False, description__startswith='Debit').exists())
        self.assertEqual(self.client.get(f'/api/admin/zatca/documents/{original.id}/').data['notes'][0]['number'], note.number)

    def test_collected_at_the_desk_records_the_payment_and_prints(self):
        original = self.sale()
        r, _, _ = self.debit(original, amount='46', reason='Wrong plan price', description='Plan price difference',
                             collected_with='cash')
        self.assertEqual(r.status_code, 201, r.data)
        txn = Transaction.objects.get(description='Debit note — Plan price difference')
        self.assertEqual((txn.amount, txn.payment_method_label, txn.member), (Decimal('46.00'), 'Cash at desk', self.member))
        self.assertEqual(ZatcaDocument.objects.get(pk=r.data['id']).transaction, txn)
        token = r.data['print_url'].rstrip('/').rsplit('/', 1)[1]
        self.assertEqual(read_token(token), ('z', r.data['id']))
        html = self.client.get(f'/api/payments/receipts/{token}/').content.decode()
        for text in ('DEBIT NOTE', 'إشعار مدين', original.number, 'Wrong plan price', '46.00'):
            self.assertIn(text, html)

    def test_company_invoice_gets_a_cleared_standard_debit_note(self):
        company = BusinessCustomer.objects.create(owner=self.member, **COMPANY)
        original = self.sale('1150.00', buyer=company)
        r, report, clear = self.debit(original, amount='115.00', reason='Extra seat not invoiced')
        note = ZatcaDocument.objects.get(pk=r.data['id'])
        self.assertEqual((note.is_standard, note.status, report.call_count, clear.call_count), (True, 'cleared', 0, 1))
        self.assertIn('<cbc:InvoiceTypeCode name="0100000">383</cbc:InvoiceTypeCode>', note.signed_xml)
        self.assertEqual(signer.invoice_hash(note.signed_xml), note.invoice_hash)

    def test_bad_input_and_permissions(self):
        original = self.sale()
        r, _, _ = self.debit(original, amount='0', reason='x', collected_with='bitcoin')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.data), {'amount', 'reason', 'collected_with'})
        note = ZatcaDocument.objects.get(pk=self.debit(original, amount='5', reason='Locker fee missed')[0].data['id'])
        r, _, _ = self.debit(note, amount='5', reason='A debit note on a debit note')
        self.assertIn('open the original invoice', r.data['detail'])
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.debit(original, amount='5', reason='Locker fee missed')[0].status_code, 403)
