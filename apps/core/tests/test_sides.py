"""The male and female gyms share one backend but never each other's data (apps.core.sides)."""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.admin_api.models import SystemSettings
from apps.members.models import MembershipPlan
from apps.shop.models import ProductCategory

User = get_user_model()
FEMALE = 'https://female-smart-gym.vercel.app'
MALE = 'https://gym-member-and-admin.vercel.app'
PASSWORD = 'Str0ng-Passw0rd!'


class SideTests(APITestCase):
    def setUp(self):
        self.male_admin = User.objects.create_user(username='ma', email='ma@t.local', password=PASSWORD, role='admin')
        self.female_admin = User.objects.create_user(username='fa', email='fa@t.local', password=PASSWORD, role='admin',
                                                     side='female')
        self.male_member = User.objects.create_user(username='mm', email='mm@t.local', password=PASSWORD)
        self.female_member = User.objects.create_user(username='fm', email='fm@t.local', password=PASSWORD, side='female')
        MembershipPlan.objects.create(name='pro', display_name='Pro (men)', price_monthly=100, price_annual=1000)
        MembershipPlan.objects.create(name='pro', display_name='Pro (women)', price_monthly=120, price_annual=1200,
                                      side='female')     # the same name is fine on the other side

    def get(self, url, origin, user=None):
        if user:
            self.client.force_authenticate(user)
        return self.client.get(url, HTTP_ORIGIN=origin)

    def login(self, email, origin):
        return self.client.post('/api/auth/login/', {'email': email, 'password': PASSWORD}, format='json',
                                HTTP_ORIGIN=origin)

    def test_each_public_website_sees_its_own_plans(self):
        self.assertEqual([p['display_name'] for p in self.get('/api/public/plans/', FEMALE).data], ['Pro (women)'])
        self.assertEqual([p['display_name'] for p in self.get('/api/public/plans/', MALE).data], ['Pro (men)'])

    def test_admins_see_only_their_gyms_members(self):
        emails = [m['email'] for m in self.get('/api/admin/members/', FEMALE, self.female_admin).data['results']]
        self.assertIn('fm@t.local', emails)
        self.assertNotIn('mm@t.local', emails)
        emails = [m['email'] for m in self.get('/api/admin/members/', MALE, self.male_admin).data['results']]
        self.assertIn('mm@t.local', emails)
        self.assertNotIn('fm@t.local', emails)

    def test_an_account_only_signs_in_on_its_own_website(self):
        self.assertEqual(self.login('fm@t.local', FEMALE).status_code, 200)
        self.assertNotEqual(self.login('fm@t.local', MALE).status_code, 200)
        self.assertNotEqual(self.login('ma@t.local', FEMALE).status_code, 200)

    def test_a_male_token_is_refused_on_the_female_website(self):
        access = self.login('ma@t.local', MALE).data
        token = access.get('access') or access.get('tokens', {}).get('access')
        self.assertTrue(token)
        r = self.client.get('/api/admin/members/', HTTP_ORIGIN=FEMALE, HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(r.status_code, 401)
        r = self.client.get('/api/admin/members/', HTTP_ORIGIN=MALE, HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(r.status_code, 200)

    def test_new_records_land_on_the_callers_side(self):
        self.client.force_authenticate(self.female_admin)
        r = self.client.post('/api/admin/shop/categories/', {'name': 'Yoga mats'}, format='json', HTTP_ORIGIN=FEMALE)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(ProductCategory._base_manager.get(name='Yoga mats').side, 'female')
        self.client.force_authenticate(self.male_admin)
        self.assertEqual(self.client.get('/api/admin/shop/categories/', HTTP_ORIGIN=MALE).data, [])

    def test_an_email_used_at_the_other_gym_is_refused(self):
        self.client.force_authenticate(self.female_admin)
        r = self.client.post('/api/admin/staff/', {'email': 'mm@t.local', 'first_name': 'Sami', 'role': 'receptionist'},
                             format='json', HTTP_ORIGIN=FEMALE)
        self.assertEqual(r.status_code, 400)
        self.assertIn('other gym', str(r.data['email']))

    def test_settings_are_kept_per_gym(self):
        from apps.core.sides import using_side
        with using_side('female'):
            s = SystemSettings.get_solo()
            s.general = {'gym_name': 'Smart Gym Ladies'}
            s.save()
        with using_side('male'):
            self.assertNotEqual(SystemSettings.get_solo().general.get('gym_name'), 'Smart Gym Ladies')
