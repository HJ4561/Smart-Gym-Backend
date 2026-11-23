"""Server-side search and filters on the list endpoints (apps.core.listing)."""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.catalog.models import Course, TrainerProfile, TrainerReview, TrainerSession
from apps.classes.models import GymClass
from apps.notifications.models import Notification
from apps.payments.models import Transaction
from apps.shop.models import Product, ProductCategory
from apps.site_content.models import ContactMessage

User = get_user_model()


class ListFilterTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='ad', email='ad@t.local', password='x', role='admin')
        self.member = User.objects.create_user(username='me', email='me@t.local', password='x', first_name='Sara')
        cu = User.objects.create_user(username='co', email='co@t.local', password='x', role='trainer', first_name='Omar')
        self.coach = TrainerProfile.objects.create(user=cu, title='Strength coach', hourly_rate=Decimal('200'),
                                                   specialties=['Powerlifting'], is_accepting_clients=True, offers_1on1=True,
                                                   years_experience=8)
        cu2 = User.objects.create_user(username='c2', email='c2@t.local', password='x', role='trainer', first_name='Lina')
        self.coach2 = TrainerProfile.objects.create(user=cu2, title='Yoga teacher', hourly_rate=Decimal('100'),
                                                    specialties=['Mobility'], years_experience=2, is_accepting_clients=False)

    def as_(self, u):
        self.client.force_authenticate(u)

    def test_admin_classes(self):
        GymClass.objects.create(name='Morning HIIT', category='hiit', level='beginner', trainer=self.coach,
                                day_of_week=0, start_time=time(7))
        GymClass.objects.create(name='Evening Yoga', category='mobility', level='advanced', trainer=self.coach2,
                                day_of_week=1, start_time=time(19))
        self.as_(self.admin)
        names = lambda **p: [c['name'] for c in self.client.get('/api/classes/admin/', p).data]   # noqa: E731
        self.assertEqual(names(q='yoga'), ['Evening Yoga'])
        self.assertEqual(names(category='hiit'), ['Morning HIIT'])
        self.assertEqual(names(level='advanced'), ['Evening Yoga'])
        self.assertEqual(names(trainer=self.coach.id), ['Morning HIIT'])

    def test_inbox_search_and_dates(self):
        ContactMessage.objects.create(name='Ali Hassan', email='ali@x.com', message='Pool hours?')
        old = ContactMessage.objects.create(name='Mona', email='mona@x.com', message='Parking')
        ContactMessage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=40))
        self.as_(self.admin)
        r = self.client.get('/api/admin/site-content/messages/', {'q': 'pool'})
        self.assertEqual([m['name'] for m in r.data['results']], ['Ali Hassan'])
        since = (timezone.localdate() - timedelta(days=7)).isoformat()
        r = self.client.get('/api/admin/site-content/messages/', {'from': since})
        self.assertEqual([m['name'] for m in r.data['results']], ['Ali Hassan'])

    def test_sessions_admin_and_coach(self):
        soon = timezone.now() + timedelta(days=1)
        TrainerSession.objects.create(member=self.member, trainer=self.coach, scheduled_at=soon, focus_area='Squat')
        TrainerSession.objects.create(member=self.member, trainer=self.coach, scheduled_at=soon + timedelta(hours=3),
                                      focus_area='Bench', status='cancelled')
        self.as_(self.admin)
        r = self.client.get('/api/admin/trainer-sessions/', {'q': 'squat'})
        self.assertEqual([s['focus_area'] for s in r.data], ['Squat'])
        r = self.client.get('/api/admin/trainer-sessions/', {'status': 'cancelled'})
        self.assertEqual([s['focus_area'] for s in r.data], ['Bench'])
        self.as_(self.coach.user)
        r = self.client.get('/api/coach/sessions/', {'q': 'bench'})
        self.assertEqual([s['focus_area'] for s in r.data], ['Bench'])

    def test_coach_courses_and_reviews(self):
        Course.objects.create(title='Deadlift clinic', description='x', category='strength', trainer=self.coach, status='draft')
        Course.objects.create(title='Squat school', description='x', category='strength', trainer=self.coach,
                              status='published')
        TrainerReview.objects.create(trainer=self.coach, member=self.member, rating=5, comment='Great', status='approved')
        self.as_(self.coach.user)
        r = self.client.get('/api/coach/courses/', {'status': 'draft'})
        self.assertEqual([c['title'] for c in r.data['courses']], ['Deadlift clinic'])
        self.assertEqual(len(self.client.get('/api/coach/reviews/', {'rating': 4}).data['reviews']), 0)
        self.assertEqual(len(self.client.get('/api/coach/reviews/', {'rating': 5}).data['reviews']), 1)

    def test_member_trainers_and_courses(self):
        self.as_(self.member)
        names = lambda **p: [t['title'] for t in self.client.get('/api/catalog/trainers/', p).data]  # noqa: E731
        self.assertEqual(names(specialty='Mobility'), ['Yoga teacher'])
        self.assertEqual(names(open='true'), ['Strength coach'])
        self.assertEqual(names(sort='price'), ['Yoga teacher', 'Strength coach'])
        self.assertEqual(names(q='yoga'), ['Yoga teacher'])
        Course.objects.create(title='Cheap', description='x', category='strength', trainer=self.coach, status='published',
                              price=Decimal('50'))
        Course.objects.create(title='Pricey', description='x', category='strength', trainer=self.coach, status='published',
                              price=Decimal('500'))
        titles = [c['title'] for c in self.client.get('/api/catalog/courses/', {'sort': '-price'}).data]
        self.assertEqual(titles, ['Pricey', 'Cheap'])

    def test_member_payments_shop_notifications(self):
        Transaction.objects.create(member=self.member, transaction_id='TXN-A1', description='Protein tub',
                                   transaction_type='retail', amount=Decimal('99'), status='settled')
        Transaction.objects.create(member=self.member, transaction_id='TXN-B2', description='Membership',
                                   transaction_type='subscription', amount=Decimal('200'), status='settled')
        cat = ProductCategory.objects.create(key='protein', name='Protein', name_ar='بروتين')
        Product.objects.create(category=cat, name='Whey Isolate', name_ar='واي معزول', sku='PRO-0001', price=Decimal('99'),
                               stock_quantity=5)
        Notification.objects.create(recipient=self.member, category='payment', title='Receipt ready', message='Thanks')
        Notification.objects.create(recipient=self.member, category='attendance', title='Checked in', message='Welcome')
        self.as_(self.member)
        r = self.client.get('/api/payments/transactions/', {'q': 'protein'})
        self.assertEqual([t['transaction_id'] for t in r.data], ['TXN-A1'])
        r = self.client.get('/api/shop/products/', {'search': 'whey'})
        self.assertEqual([p['name'] for p in r.data], ['Whey Isolate'])
        r = self.client.get('/api/notifications/', {'q': 'receipt'})
        self.assertEqual([n['title'] for n in r.data], ['Receipt ready'])
