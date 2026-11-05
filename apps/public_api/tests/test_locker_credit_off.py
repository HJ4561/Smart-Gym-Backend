"""
Locker credit switched off (the default for now): no top-ups, no paying with credit, and a cancelled
booking is refunded to the card that paid it (the front desk refunds a desk payment).
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import override_settings

from apps.catalog.models import TrainerSession
from apps.notifications.models import Notification
from apps.payments.models import LockerCredit, PaymentIntent

from . import test_member_portal as portal
from . import test_round5 as round5

User = portal.User


@override_settings(LOCKER_CREDIT_ENABLED=False)
class LockerCreditOffTests(portal.TrainerBookingTests):
    # Only the tests below (the booking helpers come from portal.TrainerBookingTests).
    test_slots_follow_availability_and_bookings = None
    test_booking_outside_hours_or_twice_is_refused = None
    test_session_scheduled_after_payment_and_refund_on_cancel = None

    def paid_session(self):
        s = self.book(9).data
        self.moyasar_pays(self.checkout(purpose='trainer_session', session_id=s['id']).data)
        return TrainerSession.objects.get(id=s['id'])

    def test_top_up_and_paying_with_credit_are_coming_soon(self):
        r = self.checkout(purpose='locker_topup', amount=50)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['detail'], 'Locker credit is coming soon.')
        LockerCredit.objects.create(member=self.user, balance=Decimal('500'))
        s = self.book(10).data
        r = self.checkout(purpose='trainer_session', session_id=s['id'], method='locker_credit')
        self.assertEqual((r.status_code, r.data['detail']), (400, 'Locker credit is coming soon.'))
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('500'))   # kept as it was
        self.assertNotIn('Locker credit', self.client.get('/api/payments/methods/').data['accepted'])
        self.assertFalse(self.client.get('/api/payments/locker-credit/').data['enabled'])

    def test_cancelling_a_card_paid_session_refunds_the_card(self):
        session = self.paid_session()
        with patch('apps.public_api.moyasar.refund_payment') as refund:
            r = self.client.post(f'/api/catalog/sessions/{session.id}/cancel/')
        self.assertEqual(r.status_code, 200)
        refund.assert_called_once_with('pay_1', 12000)
        self.assertFalse(LockerCredit.objects.filter(member=self.user, balance__gt=0).exists())
        self.assertEqual(PaymentIntent.objects.get(trainer_session=session).status, 'refunded')
        note = Notification.objects.filter(recipient=self.user, title='Session cancelled').latest('id')
        self.assertIn('refunded to your card', note.message)

    def test_failed_card_refund_keeps_the_booking(self):
        from apps.public_api.moyasar import MoyasarError
        session = self.paid_session()
        with patch('apps.public_api.moyasar.refund_payment', side_effect=MoyasarError('down')):
            r = self.client.post(f'/api/catalog/sessions/{session.id}/cancel/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('couldn’t refund your card', r.data['detail'])
        session.refresh_from_db()
        self.assertEqual(session.status, 'scheduled')

    def test_desk_payment_is_refunded_by_the_front_desk(self):
        admin = User.objects.create_user(username='adm', email='adm@t.local', password='x', role='admin')
        session = TrainerSession.objects.create(member=self.user, trainer=self.trainer, status='scheduled',
                                                scheduled_at=self.at(11), amount_charged=Decimal('120'))
        r = self.client.post(f'/api/catalog/sessions/{session.id}/cancel/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('front desk will refund',
                      Notification.objects.filter(recipient=self.user).latest('id').message)
        self.assertTrue(Notification.objects.filter(recipient=admin, title='Refund at the desk').exists())

    def test_turning_it_back_on_restores_credit_refunds(self):
        session = self.paid_session()
        with override_settings(LOCKER_CREDIT_ENABLED=True), patch('apps.public_api.moyasar.refund_payment') as refund:
            self.client.post(f'/api/catalog/sessions/{session.id}/cancel/')
        refund.assert_not_called()
        self.assertEqual(LockerCredit.objects.get(member=self.user).balance, Decimal('120.00'))


@override_settings(LOCKER_CREDIT_ENABLED=False)
class ClassSessionOffTests(round5.Base):
    def test_paid_with_credit_before_the_switch_goes_back_to_credit(self):
        from datetime import time
        from django.utils import timezone
        from apps.classes.models import GymClass
        nxt = timezone.localdate() + timedelta(days=3)
        c = GymClass.objects.create(name='Spin', category='endurance', day_of_week=nxt.weekday(), start_time=time(18, 0),
                                    capacity=10, trainer=self.coach, price_per_session=Decimal('25'))
        LockerCredit.objects.create(member=self.member, balance=Decimal('100'))
        self.as_(self.member)
        day = self.client.get('/api/classes/').data['classes'][0]['session_dates'][0]['date']
        with override_settings(LOCKER_CREDIT_ENABLED=True):
            self.client.post('/api/payments/checkout/', {'purpose': 'class_pass', 'class_id': c.id, 'kind': 'session',
                                                        'date': str(day), 'method': 'locker_credit'}, format='json')
        self.assertEqual(LockerCredit.objects.get(member=self.member).balance, Decimal('75.00'))
        r = self.client.post(f'/api/classes/{c.id}/cancel/', {'date': str(day)}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(LockerCredit.objects.get(member=self.member).balance, Decimal('100.00'))
        self.assertIn('Class session cancelled', self.titles(self.admin))
