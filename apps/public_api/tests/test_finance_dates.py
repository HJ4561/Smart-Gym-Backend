"""Finance overview for one day (?date=YYYY-MM-DD) or today (?range=1d)."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.payments import gateway
from apps.payments.models import Transaction

User = get_user_model()


class FinanceDateTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        self.member = User.objects.create_user(username='m', email='m@t.local', password='x', role='member')
        self.client.force_authenticate(self.admin)
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)
        self.pay('100.00', self.today, 10)
        self.pay('40.00', self.yesterday, 9)
        self.pay('25.00', self.yesterday, 23)
        self.pay('7.00', self.today - timedelta(days=5), 12)

    def pay(self, amount, day, hour):
        txn = gateway.record_transaction(self.member, 'Course', 'coaching', Decimal(amount), 'mada', f'p{amount}',
                                         email=False, invoice=False)
        when = timezone.make_aware(datetime.combine(day, time(hour, 30)))
        if when > timezone.now():
            when = timezone.now() - timedelta(minutes=1)
        Transaction.objects.filter(pk=txn.pk).update(created_at=when)

    def overview(self, query):
        return self.client.get(f'/api/admin/finance/overview/?{query}')

    def test_one_day(self):
        r = self.overview(f'date={self.yesterday.isoformat()}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.data['received'], r.data['payment_count'], r.data['range_label']),
                         (65.0, 2, self.yesterday.isoformat()))
        self.assertEqual(self.overview(f'date={self.today.isoformat()}').data['received'], 100.0)

    def test_today_and_rolling_ranges(self):
        self.assertEqual((self.overview('range=1d').data['received'], self.overview('range=1d').data['range_label']),
                         (100.0, 'Today'))
        self.assertEqual(self.overview('range=7d').data['received'], 172.0)

    def test_bad_or_future_date(self):
        r = self.overview('date=05-11-2026')
        self.assertEqual(r.status_code, 400)
        self.assertIn('YYYY-MM-DD', str(r.data['date']))
        r = self.overview(f'date={(self.today + timedelta(days=1)).isoformat()}', )
        self.assertEqual(r.status_code, 400)
        self.client.credentials(HTTP_ACCEPT_LANGUAGE='ar')
        self.assertIn('استخدم صيغة التاريخ', self.overview('date=nope').content.decode())
