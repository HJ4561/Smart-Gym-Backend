"""Gate / entry tests: FaceCheck audit trail, check-in plan validation, session timer."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.members.models import CheckIn, FaceCheck, Membership, MembershipPlan

User = get_user_model()
BASE = '/api/admin/registrations-api/members'


class GateTestBase(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='gate', email='gate@test.local', password='x', is_staff=True)
        self.member = User.objects.create_user(username='m1', email='m1@test.local', password='x')
        self.other = User.objects.create_user(username='m2', email='m2@test.local', password='x')
        self.plan = MembershipPlan.objects.create(
            name='pro', display_name='Pro', price_monthly=79, price_annual=790, session_limit_minutes=90,
        )
        self.membership = self.make_membership(self.member, '2999888777')
        self.client.force_authenticate(self.staff)

    def make_membership(self, user, iqama, plan=None, **overrides):
        fields = dict(member=user, plan=plan or self.plan, start_date=date.today(),
                      expiry_date=date.today() + timedelta(days=30), iqama_number=iqama,
                      id_verified_at=timezone.now())   # first-visit ID check done (see FirstVisitTests)
        fields.update(overrides)
        return Membership.objects.create(**fields)

    def face_check(self, user, match, confidence=0.877, threshold=0.363):
        return self.client.post(f'{BASE}/{user.id}/face-verify/',
                                {'match': match, 'confidence': confidence, 'threshold': threshold}, format='json')

    def check_in(self, user, **body):
        return self.client.post(f'{BASE}/{user.id}/check-in/', {'facility': 'Main Entrance', **body}, format='json')


class FaceCheckTests(GateTestBase):
    def test_denied_attempt_is_recorded(self):
        r = self.face_check(self.member, False, confidence=0.21)
        self.assertEqual(r.status_code, 201)
        fc = FaceCheck.objects.get(id=r.data['face_check_id'])
        self.assertFalse(fc.matched)
        self.assertEqual(fc.iqama_number, '2999888777')
        self.assertEqual(fc.confidence, 0.21)
        self.assertEqual(fc.threshold, 0.363)
        self.assertEqual(fc.verified_by, self.staff)

    def test_granted_check_in_links_its_face_check(self):
        fc_id = self.face_check(self.member, True).data['face_check_id']
        r = self.check_in(self.member, face_check_id=fc_id)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(FaceCheck.objects.get(id=fc_id).check_in_id, r.data['id'])
        self.assertTrue(r.data['face_verified'])

    def test_check_in_refuses_denied_face_check(self):
        fc_id = self.face_check(self.member, False).data['face_check_id']
        self.assertEqual(self.check_in(self.member, face_check_id=fc_id).status_code, 400)

    def test_check_in_refuses_another_members_face_check(self):
        self.make_membership(self.other, '2888777666')
        fc_id = self.face_check(self.member, True).data['face_check_id']
        self.assertEqual(self.check_in(self.other, face_check_id=fc_id).status_code, 400)

    def test_face_check_cannot_be_reused(self):
        fc_id = self.face_check(self.member, True).data['face_check_id']
        self.assertEqual(self.check_in(self.member, face_check_id=fc_id).status_code, 201)
        CheckIn.objects.update(check_out_time='2026-01-01T00:00:00Z')
        self.assertEqual(self.check_in(self.member, face_check_id=fc_id).status_code, 400)

    def test_rejects_malformed_bodies(self):
        for body in ({'match': 'yes', 'confidence': 0.5}, {'match': True}, {'match': True, 'confidence': 'abc'}):
            with self.subTest(body=body):
                r = self.client.post(f'{BASE}/{self.member.id}/face-verify/', body, format='json')
                self.assertEqual(r.status_code, 400)

    def test_requires_staff(self):
        self.client.force_authenticate(None)
        self.assertIn(self.face_check(self.member, True).status_code, (401, 403))
        self.client.force_authenticate(self.member)   # a plain member
        self.assertIn(self.face_check(self.member, True).status_code, (401, 403))

    def test_audit_row_survives_member_deletion(self):
        fc_id = self.face_check(self.member, False).data['face_check_id']
        self.member.delete()
        fc = FaceCheck.objects.get(id=fc_id)
        self.assertIsNone(fc.member_id)
        self.assertEqual(fc.iqama_number, '2999888777')


class CheckInPlanTests(GateTestBase):
    def test_session_timer_from_plan_limit(self):
        r = self.check_in(self.member)
        self.assertEqual(r.status_code, 201)
        ci = CheckIn.objects.get(id=r.data['id'])
        self.assertEqual(ci.session_limit_minutes, 90)
        self.assertEqual(ci.expected_checkout_time - ci.check_in_time, timedelta(minutes=90))
        self.assertEqual(r.data['session_limit_minutes'], 90)
        self.assertIsNotNone(r.data['expected_checkout_time'])

    def test_unlimited_plan_has_no_expected_checkout(self):
        self.plan.session_limit_minutes = None
        self.plan.save()
        r = self.check_in(self.member)
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(r.data['session_limit_minutes'])
        self.assertIsNone(r.data['expected_checkout_time'])

    def test_limit_is_snapshotted_at_check_in(self):
        ci_id = self.check_in(self.member).data['id']
        self.plan.session_limit_minutes = 30
        self.plan.save()
        self.assertEqual(CheckIn.objects.get(id=ci_id).session_limit_minutes, 90)

    def test_refuses_member_without_membership(self):
        r = self.check_in(self.other)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(CheckIn.objects.filter(member=self.other).exists())

    def test_refuses_invalid_membership(self):
        cases = {
            'expired': {'expiry_date': date.today() - timedelta(days=1)},
            'frozen': {'is_frozen': True},
            'status expired': {'status': 'expired'},
            'status frozen': {'status': 'frozen'},
        }
        for label, change in cases.items():
            with self.subTest(label):
                valid = {'expiry_date': date.today() + timedelta(days=30), 'is_frozen': False, 'status': 'active'}
                Membership.objects.filter(id=self.membership.id).update(**{**valid, **change})
                r = self.check_in(self.member)
                self.assertEqual(r.status_code, 400, r.data)
                self.assertEqual(r.data['detail'], 'Membership is not valid for entry.')
        self.assertFalse(CheckIn.objects.exists())

    def test_expiring_soon_membership_still_allowed(self):
        Membership.objects.filter(id=self.membership.id).update(status='expiring')
        self.assertEqual(self.check_in(self.member).status_code, 201)

    def test_increments_total_check_ins(self):
        self.check_in(self.member)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.total_check_ins, 1)

    def test_double_check_in_refused(self):
        self.assertEqual(self.check_in(self.member).status_code, 201)
        self.assertEqual(self.check_in(self.member).status_code, 400)
