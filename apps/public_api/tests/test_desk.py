"""Front desk (receptionist portal): module access, first-visit ID check, walk-in signup."""
import shutil
import tempfile
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.members.models import CheckIn, Membership, MembershipPlan, MembershipRegistration
from apps.payments.models import Transaction
from apps.site_content.models import ContactMessage
from apps.staff.models import StaffProfile
from .faces import one_face
from .test_payments import photo

User = get_user_model()
_faces = one_face()
_media = tempfile.mkdtemp()


def setUpModule():
    _faces.start()


def tearDownModule():
    _faces.stop()
    shutil.rmtree(_media, ignore_errors=True)


class DeskBase(APITestCase):
    def setUp(self):
        self.plan = MembershipPlan.objects.create(name='pro', display_name='Pro', price_monthly=100, price_annual=1000)
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        self.member = User.objects.create_user(username='mem', email='mem@t.local', password='x')
        self.membership = Membership.objects.create(member=self.member, plan=self.plan, start_date=date.today(),
                                                    expiry_date=date.today() + timedelta(days=30),
                                                    iqama_number='2111222333')

    def desk(self, *modules):
        u = User.objects.create_user(username=f'r{len(modules)}{"".join(modules)}', email=f'r{"".join(modules)}@t.local',
                                     password='x', role='receptionist')
        StaffProfile.objects.update_or_create(user=u, defaults={'modules': list(modules)})
        u = User.objects.get(pk=u.pk)
        self.client.force_authenticate(u)
        return u


class ModuleAccessTests(DeskBase):
    def test_members_module_reads_but_never_edits(self):
        self.desk('members')
        self.assertEqual(self.client.get('/api/admin/members/').status_code, 200)
        self.assertEqual(self.client.get(f'/api/admin/members/{self.member.id}/').status_code, 200)
        r = self.client.patch(f'/api/admin/members/{self.member.id}/update/', {'phone': '0500000000'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_without_the_module_nothing(self):
        self.desk('gate')
        self.assertEqual(self.client.get('/api/admin/members/').status_code, 403)
        self.assertEqual(self.client.get('/api/admin/site-content/messages/').status_code, 403)
        self.assertEqual(self.client.get('/api/admin/registrations-api/registrations/').status_code, 403)

    def test_inbox_reads_and_marks_but_cannot_delete(self):
        msg = ContactMessage.objects.create(name='A B', email='a@b.com', message='Hello there')
        self.desk('inbox')
        self.assertEqual(self.client.get('/api/admin/site-content/messages/').status_code, 200)
        r = self.client.patch(f'/api/admin/site-content/messages/{msg.id}/', {'status': 'read'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.delete(f'/api/admin/site-content/messages/{msg.id}/').status_code, 403)

    def test_classes_module_reads_timetable_but_cannot_add(self):
        self.desk('classes')
        self.assertEqual(self.client.get('/api/classes/admin/').status_code, 200)
        self.assertEqual(self.client.post('/api/classes/admin/', {'name': 'X'}, format='json').status_code, 403)

    def test_desk_home_counts_only_their_modules(self):
        self.desk('gate')
        r = self.client.get('/api/admin/desk/today/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['modules'], ['gate'])
        self.assertIn('first_visits_waiting', r.data)
        self.assertNotIn('messages_new', r.data)


class FirstVisitTests(DeskBase):
    def check_in(self, **body):
        return self.client.post(f'/api/admin/registrations-api/members/{self.member.id}/check-in/', body, format='json')

    def test_first_check_in_waits_for_the_id_check(self):
        self.desk('gate')
        r = self.check_in()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['reason'], 'first_visit')
        r = self.check_in(id_checked=True)
        self.assertEqual(r.status_code, 201)
        self.membership.refresh_from_db()
        self.assertIsNotNone(self.membership.id_verified_at)
        self.assertFalse(self.membership.needs_first_visit_check)

    def test_verify_identity_needs_both_ticks(self):
        self.desk('gate')
        url = f'/api/admin/members/{self.member.id}/verify-identity/'
        self.assertEqual(self.client.post(url, {'iqama_checked': True}, format='json').status_code, 400)
        r = self.client.post(url, {'iqama_checked': True, 'photo_checked': True}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data['membership']['needs_first_visit_check'])
        self.assertEqual(self.check_in().status_code, 201)

    def test_members_who_already_trained_count_as_checked(self):
        Membership.objects.filter(pk=self.membership.pk).update(total_check_ins=4)
        self.desk('gate')
        self.assertEqual(self.check_in().status_code, 201)
        self.assertEqual(CheckIn.objects.filter(member=self.member).count(), 1)


@override_settings(MEDIA_ROOT=_media)
class WalkInTests(DeskBase):
    def form(self, **over):
        data = {'full_name': 'Sara Ahmed', 'email': 'sara@example.com', 'phone': '0551234567',
                'iqama_number': '2999000111', 'age': 28, 'gender': 'female', 'address': 'Riyadh',
                'plan': self.plan.id, 'duration_months': 2, 'payment': 'cash', 'photo': photo()}
        data.update(over)
        return data

    def test_walk_in_is_a_member_at_once_with_a_paid_receipt(self):
        self.desk('walk_in')
        q = self.client.post('/api/admin/desk/walk-in/quote/', {'plan': self.plan.id, 'duration_months': 2},
                             format='json')
        self.assertEqual(q.data['total'], '200.00')
        r = self.client.post('/api/admin/desk/walk-in/', self.form(), format='multipart')
        self.assertEqual(r.status_code, 201, r.data)
        user = User.objects.get(email='sara@example.com')
        self.assertEqual(user.role, 'member')
        self.assertTrue(user.must_change_password)          # temporary password emailed
        m = Membership.objects.get(member=user)
        self.assertEqual(m.status, 'active')
        self.assertFalse(m.needs_first_visit_check)         # seen in person at the desk
        reg = MembershipRegistration.objects.get(email='sara@example.com')
        self.assertEqual(reg.status, MembershipRegistration.STATUS_APPROVED)
        txn = Transaction.objects.get(member=user)
        self.assertEqual(str(txn.amount), '200.00')
        self.assertEqual(txn.payment_method_label, 'Cash at desk')

    def test_walk_in_needs_a_desk_payment_and_the_module(self):
        self.desk('walk_in')
        r = self.client.post('/api/admin/desk/walk-in/', self.form(payment='mada'), format='multipart')
        self.assertEqual(r.status_code, 400)
        self.assertIn('payment', r.data)
        self.desk('gate')
        r = self.client.post('/api/admin/desk/walk-in/', self.form(), format='multipart')
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.filter(email='sara@example.com').exists())
