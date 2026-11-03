"""
Fifth round: free 1-on-1 sessions skip payment, a member can keep plans with several paid
nutritionists, class cancellations reach the coach and the admin portal, and changing the password
keeps the browser that made the change trusted.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts import two_factor
from apps.accounts.models import TrustedDevice, TwoFactor
from apps.catalog.models import TrainerAvailability, TrainerProfile, TrainerSession
from apps.classes.models import ClassEnrollment, GymClass
from apps.members.models import Membership, MembershipPlan
from apps.notifications.models import Notification
from apps.nutrition.models import NutritionistProfile, NutritionPlan
from apps.payments.models import LockerCredit

User = get_user_model()
PW = 'Strong-Pass-123'


class Base(APITestCase):
    def setUp(self):
        plan = MembershipPlan.objects.create(name='basic', display_name='Basic', price_monthly=50, price_annual=500)
        self.member = User.objects.create_user(username='m', email='m@t.local', password=PW, role='member',
                                               first_name='Sara', last_name='Ali')
        Membership.objects.create(member=self.member, plan=plan, start_date=timezone.localdate(),
                                  expiry_date=timezone.localdate() + timedelta(days=60), iqama_number='2111111111')
        self.admin = User.objects.create_user(username='a', email='a@t.local', password=PW, role='admin')
        self.coach_user = User.objects.create_user(username='c', email='c@t.local', password=PW, role='trainer',
                                                   first_name='Omar')
        self.coach = TrainerProfile.objects.create(user=self.coach_user, title='Coach', hourly_rate=Decimal('0'))

    def as_(self, user):
        self.client.force_authenticate(user)

    def titles(self, user):
        return list(Notification.objects.filter(recipient=user).values_list('title', flat=True))


class FreeSessionTests(Base):
    def setUp(self):
        super().setUp()
        self.day = timezone.localdate() + timedelta(days=2)
        TrainerAvailability.objects.create(trainer=self.coach, day_of_week=self.day.weekday(),
                                           start_time=time(9, 0), end_time=time(12, 0))

    def book(self):
        at = timezone.make_aware(datetime.combine(self.day, time(9, 0)), timezone.get_current_timezone())
        return self.client.post('/api/catalog/sessions/', {'trainer_id': self.coach.id, 'scheduled_at': at.isoformat()},
                                format='json')

    def test_a_free_session_is_booked_without_payment(self):
        self.as_(self.member)
        r = self.book()
        self.assertEqual((r.status_code, r.data['status']), (201, 'scheduled'))
        self.assertIn('Session booked', self.titles(self.member))
        self.assertIn('New 1-on-1 booking', self.titles(self.coach_user))

    def test_a_paid_session_still_waits_for_payment(self):
        TrainerProfile.objects.filter(id=self.coach.id).update(hourly_rate=Decimal('120'))
        self.as_(self.member)
        self.assertEqual(self.book().data['status'], 'pending_payment')


class SeveralNutritionistsTests(Base):
    def nutritionist(self, name, price):
        u = User.objects.create_user(username=name, email=f'{name}@t.local', password=PW, role='nutritionist',
                                     first_name=name.title())
        return NutritionistProfile.objects.create(user=u, title='Dietitian', hourly_rate=Decimal(price))

    def test_every_paid_nutritionist_keeps_an_active_plan(self):
        huda, amal = self.nutritionist('huda', '0'), self.nutritionist('amal', '50')
        LockerCredit.objects.create(member=self.member, balance=Decimal('100'))
        self.as_(self.member)
        self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': huda.id, 'goal': 'fat_loss'}, format='json')
        r = self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': amal.id, 'goal': 'hypertrophy'},
                             format='json')
        paid = self.client.post('/api/payments/checkout/', {'purpose': 'nutrition_plan', 'plan_id': r.data['id'],
                                                            'method': 'locker_credit'}, format='json')
        self.assertIn(paid.status_code, (200, 201), paid.data)
        plans = self.client.get('/api/nutrition/plans/').data
        self.assertEqual([p['nutritionist_name'] for p in plans], ['Amal', 'Huda'])          # newest first
        self.assertEqual(self.client.get('/api/nutrition/plan/').data['nutritionist_name'], 'Amal')
        # both nutritionists see the member's meals on the admin side
        self.client.post('/api/nutrition/meals/', {'meal_type': 'lunch', 'description': 'Rice', 'calories': 500},
                         format='json')
        self.as_(self.admin)
        rows = self.client.get('/api/admin/nutrition/plans/', {'status': 'active'}).data
        self.assertEqual(sorted(r['nutritionist'] for r in rows), ['Amal', 'Huda'])
        days = self.client.get(f'/api/admin/nutrition/members/{self.member.id}/meals/').data
        self.assertEqual(days[0]['meals'][0]['description'], 'Rice')

    def test_an_unpaid_request_doesnt_cancel_another_nutritionists_request(self):
        a, b = self.nutritionist('huda', '30'), self.nutritionist('amal', '50')
        self.as_(self.member)
        self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': a.id, 'goal': 'fat_loss'}, format='json')
        self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': b.id, 'goal': 'fat_loss'}, format='json')
        self.assertEqual(NutritionPlan.objects.filter(member=self.member, status='pending_payment').count(), 2)


class ClassCancellationTests(Base):
    def make(self, **kw):
        nxt = timezone.localdate() + timedelta(days=3)
        return GymClass.objects.create(name='Spin', category='endurance', day_of_week=nxt.weekday(),
                                       start_time=time(18, 0), capacity=10, trainer=self.coach, **kw)

    def test_member_cancelling_tells_the_coach_and_admins(self):
        c = self.make()
        self.as_(self.member)
        self.client.post(f'/api/classes/{c.id}/book/')
        self.client.post(f'/api/classes/{c.id}/cancel/')
        self.assertIn('Class booking cancelled', self.titles(self.coach_user))
        self.assertIn('Class booking cancelled', self.titles(self.admin))

    def test_paid_session_cancel_tells_the_coach_and_admins(self):
        c = self.make(price_per_session=Decimal('25'))
        LockerCredit.objects.create(member=self.member, balance=Decimal('100'))
        self.as_(self.member)
        day = self.client.get('/api/classes/').data['classes'][0]['session_dates'][0]['date']
        self.client.post('/api/payments/checkout/', {'purpose': 'class_pass', 'class_id': c.id, 'kind': 'session',
                                                    'date': str(day), 'method': 'locker_credit'}, format='json')
        r = self.client.post(f'/api/classes/{c.id}/cancel/', {'date': str(day)}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('Class session cancelled', self.titles(self.coach_user))
        self.assertIn('Class session cancelled', self.titles(self.admin))

    def test_admin_removing_a_class_or_a_booking_tells_the_coach(self):
        c = self.make()
        e = ClassEnrollment.objects.create(gym_class=c, member=self.member, status='enrolled')
        self.as_(self.admin)
        self.client.post(f'/api/classes/admin/{c.id}/roster/{e.id}/remove/')
        self.assertIn('Class booking cancelled', self.titles(self.coach_user))
        self.client.delete(f'/api/classes/admin/{c.id}/')
        self.assertIn('Class cancelled', self.titles(self.coach_user))


class TrustedDeviceSurvivesPasswordChangeTests(Base):
    def test_the_browser_changing_the_password_stays_trusted(self):
        secret = two_factor.new_secret()
        TwoFactor.objects.create(user=self.coach_user, method='totp', secret=secret, enabled=True)
        ticket = self.client.post('/api/auth/login/', {'email': 'c@t.local', 'password': PW}, format='json').data['ticket']
        r = self.client.post('/api/auth/2fa/verify/', {'ticket': ticket, 'code': two_factor.totp_now(secret),
                                                       'remember_device': True}, format='json')
        mine = r.data['device_token']
        other = two_factor.trust_device(self.coach_user, 'Other laptop')
        self.as_(self.coach_user)
        self.client.post('/api/auth/change-password/', {'old_password': PW, 'new_password': 'Another-Pass-456',
                                                         'new_password_confirm': 'Another-Pass-456',
                                                         'device_token': mine}, format='json')
        self.as_(None)
        self.assertIn('access', self.client.post('/api/auth/login/', {
            'email': 'c@t.local', 'password': 'Another-Pass-456', 'device_token': mine}, format='json').data)
        self.assertEqual(self.client.post('/api/auth/login/', {
            'email': 'c@t.local', 'password': 'Another-Pass-456', 'device_token': other}, format='json').data['two_factor'],
            'verify')
        self.assertEqual(TrustedDevice.objects.filter(user=self.coach_user).count(), 1)
