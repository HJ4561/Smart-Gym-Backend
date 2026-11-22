"""Nutritionist portal: own clients only, plans go straight to the member, profile changes wait for an admin."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.approvals.models import ChangeRequest
from apps.approvals.registry import approve
from apps.notifications.models import Notification
from apps.nutrition.models import NutritionistProfile, NutritionPlan

User = get_user_model()


class NutritionistPortalTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        self.nu_user = User.objects.create_user(username='nu', email='nu@t.local', password='x', role='nutritionist',
                                                first_name='Nora', last_name='Diet')
        self.me = NutritionistProfile.objects.create(user=self.nu_user, title='Dietitian', hourly_rate=Decimal('150'))
        other_user = User.objects.create_user(username='ot', email='ot@t.local', password='x', role='nutritionist')
        self.other = NutritionistProfile.objects.create(user=other_user, title='Coach')
        self.client_a = User.objects.create_user(username='ca', email='ca@t.local', password='x', first_name='Sara')
        self.client_b = User.objects.create_user(username='cb', email='cb@t.local', password='x', first_name='Lina')
        self.plan = NutritionPlan.objects.create(member=self.client_a, nutritionist=self.me, status='active')
        NutritionPlan.objects.create(member=self.client_b, nutritionist=self.other, status='active')
        self.client.force_authenticate(self.nu_user)

    def test_sees_only_own_clients(self):
        rows = self.client.get('/api/nutritionist/clients/').data
        self.assertEqual([r['email'] for r in rows], ['ca@t.local'])
        self.assertEqual(self.client.get(f'/api/nutritionist/clients/{self.client_b.id}/').status_code, 404)
        r = self.client.get(f'/api/nutritionist/clients/{self.client_a.id}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['member']['email'], 'ca@t.local')
        self.assertEqual(self.client.get('/api/nutritionist/me/').data['stats']['to_review'], 1)

    def test_plan_changes_go_straight_to_the_member(self):
        r = self.client.patch(f'/api/nutritionist/plans/{self.plan.id}/', {'target_calories': 2100}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.target_calories, 2100)
        self.assertIsNotNone(self.plan.reviewed_at)
        self.assertTrue(Notification.objects.filter(recipient=self.client_a, category='nutrition').exists())
        other_plan = NutritionPlan.objects.get(member=self.client_b)
        self.assertEqual(self.client.patch(f'/api/nutritionist/plans/{other_plan.id}/', {'target_calories': 2100},
                                           format='json').status_code, 404)

    def test_scan_for_own_client(self):
        r = self.client.post(f'/api/nutritionist/clients/{self.client_a.id}/scans/',
                             {'weight_kg': 70, 'body_fat_percent': 20, 'lean_mass_kg': 52, 'fat_mass_kg': 14}, format='json')
        self.assertEqual(r.status_code, 201, r.data)

    def test_profile_change_waits_for_the_admin(self):
        r = self.client.patch('/api/nutritionist/me/', {'bio': 'Sports nutrition for busy people.', 'hourly_rate': 200,
                                                        'is_accepting_clients': False}, format='json')
        self.assertEqual(r.status_code, 202, r.data)
        self.me.refresh_from_db()
        self.assertEqual((self.me.bio, self.me.hourly_rate, self.me.is_accepting_clients), ('', Decimal('150.00'), False))
        self.assertEqual(r.data['pending_change']['bio'], 'Sports nutrition for busy people.')
        approve(ChangeRequest.objects.get(target_type='nutrition.profile'), self.admin, payload={
            'bio': 'Sports nutrition for busy people.', 'hourly_rate': 180})      # admin adjusts the rate
        self.me.refresh_from_db()
        self.assertEqual((self.me.bio, self.me.hourly_rate), ('Sports nutrition for busy people.', Decimal('180.00')))

    def test_profile_cannot_change_verification(self):
        self.client.patch('/api/nutritionist/me/', {'is_verified': True}, format='json')
        self.me.refresh_from_db()
        self.assertFalse(self.me.is_verified)

    def test_other_roles_are_refused(self):
        self.client.force_authenticate(self.client_a)
        self.assertEqual(self.client.get('/api/nutritionist/me/').status_code, 403)
