"""Admin → Members → Delete: unpaid members go completely; paid ones are wiped but their tax records stay."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.catalog.models import Course, CourseReview, TrainerProfile
from apps.members.models import CheckIn, Membership, MembershipPlan, MembershipRegistration
from apps.payments.models import Transaction
from apps.staff.models import StaffProfile

User = get_user_model()
PASSWORD = 'Str0ng-Passw0rd!'


class MemberDeleteTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        self.plan = MembershipPlan.objects.create(name='pro', display_name='Pro', price_monthly=100, price_annual=1000)
        self.client.force_authenticate(self.admin)

    def member(self, email, paid=False):
        u = User.objects.create_user(username=email, email=email, password=PASSWORD, first_name='Sara', last_name='Ahmed',
                                     phone='0551234567')
        Membership.objects.create(member=u, plan=self.plan, start_date=date.today(),
                                  expiry_date=date.today() + timedelta(days=30), iqama_number='2111222333')
        MembershipRegistration.objects.create(iqama_number=f'2{u.id:09d}', full_name='Sara Ahmed', email=email,
                                              phone='0551234567', age=28, gender='female', plan=self.plan,
                                              created_user=u, status='approved')
        CheckIn.objects.create(member=u, facility='Main')
        if paid:
            Transaction.objects.create(member=u, transaction_id=f'TXN-{u.id}', description='Membership',
                                       transaction_type='subscription', amount=Decimal('100'), status='settled')
        return u

    def test_unpaid_member_is_deleted_completely(self):
        u = self.member('a@t.local')
        r = self.client.delete(f'/api/admin/members/{u.id}/')
        self.assertEqual((r.status_code, r.data['outcome']), (200, 'deleted'))
        self.assertFalse(User._base_manager.filter(id=u.id).exists())
        self.assertFalse(MembershipRegistration._base_manager.filter(email='a@t.local').exists())

    def test_paid_member_is_wiped_but_payments_stay(self):
        u = self.member('b@t.local', paid=True)
        coach = TrainerProfile.objects.create(user=User.objects.create_user(username='c', email='c@t.local', role='trainer'),
                                              title='Coach')
        course = Course.objects.create(title='Strength', description='x', category='strength', trainer=coach,
                                       status='published')
        CourseReview.objects.create(course=course, member=u, rating=5, comment='Great', status='approved')
        r = self.client.delete(f'/api/admin/members/{u.id}/')
        self.assertEqual((r.status_code, r.data['outcome']), (200, 'erased'))
        u = User._base_manager.get(id=u.id)
        self.assertEqual((u.email, u.first_name, u.phone, u.is_active, u.has_usable_password()),
                         (f'deleted-{u.id}@deleted.invalid', 'Deleted', None, False, False))
        self.assertEqual(Transaction._base_manager.filter(member=u).count(), 1)          # kept for ZATCA
        self.assertFalse(CheckIn._base_manager.filter(member=u).exists())
        self.assertFalse(CourseReview._base_manager.filter(member=u).exists())
        self.assertEqual(Membership._base_manager.get(member=u).iqama_number, '')
        reg = MembershipRegistration._base_manager.get(created_user=u)
        self.assertEqual((reg.full_name, reg.phone), ('Deleted member', ''))
        self.assertTrue(reg.iqama_number.startswith('DEL'))
        self.client.force_authenticate(None)
        r = self.client.post('/api/auth/login/', {'email': 'b@t.local', 'password': PASSWORD}, format='json')
        self.assertNotEqual(r.status_code, 200)

    def test_only_admins_delete_and_only_members(self):
        u = self.member('d@t.local')
        desk = User.objects.create_user(username='r', email='r@t.local', password='x', role='receptionist')
        StaffProfile.objects.create(user=desk, modules=['members'])
        self.client.force_authenticate(User.objects.get(pk=desk.pk))
        self.assertEqual(self.client.delete(f'/api/admin/members/{u.id}/').status_code, 403)
        self.assertEqual(self.client.get(f'/api/admin/members/{u.id}/').status_code, 200)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f'/api/admin/members/{desk.id}/').status_code, 400)
