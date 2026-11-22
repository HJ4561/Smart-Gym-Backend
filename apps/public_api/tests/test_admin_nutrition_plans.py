"""Admin → Nutrition: create a plan for a member, delete an unpaid one, archive (not delete) a paid one."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.notifications.models import Notification
from apps.nutrition.models import NutritionistProfile, NutritionPlan
from apps.payments.models import PaymentIntent

User = get_user_model()


class AdminNutritionPlanTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        self.member = User.objects.create_user(username='me', email='me@t.local', password='x', first_name='Sara')
        nu = User.objects.create_user(username='nu', email='nu@t.local', password='x', role='nutritionist', first_name='Nora')
        self.n = NutritionistProfile.objects.create(user=nu, title='Dietitian', hourly_rate=Decimal('150'))
        self.client.force_authenticate(self.admin)

    def create(self, **extra):
        body = {'member_id': self.member.id, 'nutritionist_id': self.n.id, 'goal': 'fat_loss',
                'target_calories': 1900, 'target_protein_g': 140, **extra}
        return self.client.post('/api/admin/nutrition/plans/', body, format='json')

    def test_create_is_active_free_and_tells_the_member(self):
        r = self.create()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['status'], r.data['price'], r.data['target_calories'], r.data['goal']),
                         ('active', 0.0, 1900, 'fat_loss'))
        self.assertTrue(Notification.objects.filter(recipient=self.member, category='nutrition').exists())
        self.assertEqual(self.create().status_code, 400)                  # one active plan per nutritionist

    def test_create_checks_inputs(self):
        r = self.client.post('/api/admin/nutrition/plans/', {'target_calories': 50}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('member_id', r.data)
        self.assertIn('nutritionist_id', r.data)
        r = self.create(target_calories=50)
        self.assertEqual(r.status_code, 400)
        self.assertIn('target_calories', r.data)
        self.assertFalse(NutritionPlan.objects.exists())

    def test_delete_unpaid_but_not_paid(self):
        plan = NutritionPlan.objects.get(id=self.create().data['id'])
        self.assertEqual(self.client.delete(f'/api/admin/nutrition/plans/{plan.id}/').status_code, 204)
        paid = NutritionPlan.objects.create(member=self.member, nutritionist=self.n, status='active', price=Decimal('150'))
        PaymentIntent.objects.create(member=self.member, purpose='nutrition_plan', nutrition_plan=paid,
                                     amount=Decimal('150'), status='paid')
        r = self.client.delete(f'/api/admin/nutrition/plans/{paid.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Archived', r.data['detail'])

    def test_only_admins(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.create().status_code, 403)
