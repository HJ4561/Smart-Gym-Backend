"""
Third round: course progress on the cards, new admins start without old badges, staff delete,
fixed SKU format, 2-step verification, the coach portal (courses for review, hours, sessions,
students, videos), Machine Guide video tiers, paid classes and course-purchase notices.
"""
import importlib
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.test import override_settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts import two_factor
from apps.accounts.models import TwoFactor
from apps.catalog.models import Course, CourseModule, Enrollment, TrainerProfile, TrainerSession
from apps.classes.models import ClassEnrollment, ClassPass, GymClass
from apps.members.models import Membership, MembershipPlan
from apps.notifications.models import Notification
from apps.payments.models import LockerCredit
from apps.shop.models import Product, ProductCategory
from apps.staff.models import StaffProfile
from apps.workouts.models import Exercise, ExerciseVideo, VideoPurchase

User = get_user_model()
PW = 'Strong-Pass-123'


class Base(APITestCase):
    def setUp(self):
        self.plan = MembershipPlan.objects.create(name='basic', display_name='Basic', price_monthly=50, price_annual=500)
        self.member = User.objects.create_user(username='m', email='m@t.local', password=PW, role='member',
                                               first_name='Sara', last_name='Ali')
        self.ms = Membership.objects.create(member=self.member, plan=self.plan, start_date=timezone.localdate(),
                                            expiry_date=timezone.localdate() + timedelta(days=60), iqama_number='2111111111')
        self.admin = User.objects.create_user(username='a', email='a@t.local', password=PW, role='admin', first_name='Ad')
        self.coach_user = User.objects.create_user(username='c', email='c@t.local', password=PW, role='trainer',
                                                   first_name='Omar', last_name='Saleh')
        self.coach = TrainerProfile.objects.create(user=self.coach_user, title='Coach', hourly_rate=Decimal('100'))

    def as_(self, user):
        self.client.force_authenticate(user)

    def course(self, **kw):
        c = Course.objects.create(trainer=self.coach, title=kw.pop('title', 'Strength Base'), description='Lift.',
                                  category='Strength', status=kw.pop('status', 'published'), **kw)
        for n in range(1, 5):
            CourseModule.objects.create(course=c, order=n, title=f'Day {n}', day_number=n)
        return c

    def credit(self, amount):
        LockerCredit.objects.update_or_create(member=self.member, defaults={'balance': Decimal(amount)})

    def pay(self, body):
        return self.client.post('/api/payments/checkout/', {**body, 'method': 'locker_credit'}, format='json')


# ------------------------------------------------------------------ quick fixes
class CourseProgressTests(Base):
    def test_cards_show_the_real_percentage_after_day_one(self):
        c = self.course()
        e = Enrollment.objects.create(member=self.member, course=c)
        self.as_(self.member)
        day1 = c.modules.order_by('order').first()
        self.client.post(f'/api/catalog/enrollments/{e.id}/modules/{day1.id}/complete/')
        card = self.client.get('/api/catalog/enrollments/').data[0]
        self.assertEqual((card['progress_percent'], card['completed_count'], card['total_modules']), (25, 1, 4))
        self.assertEqual(self.client.get(f'/api/catalog/courses/{c.id}/player/').data['enrollment']['progress_percent'], 25)
        CourseModule.objects.create(course=c, order=5, title='Day 5', day_number=5)      # a lesson added later
        self.assertEqual(self.client.get('/api/catalog/enrollments/').data[0]['progress_percent'], 20)


class StaffDeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.as_(self.admin)

    def test_member_on_the_team_goes_back_to_member(self):
        self.member.role = 'receptionist'
        self.member.save()
        StaffProfile.objects.create(user=self.member, department='front_desk')
        r = self.client.delete(f'/api/admin/staff/{self.member.id}/')
        self.assertEqual(r.status_code, 200, r.data)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, 'member')
        self.assertFalse(StaffProfile.objects.filter(user=self.member).exists())

    def test_plain_staff_is_deleted_but_not_coaches_with_history_or_yourself(self):
        r = User.objects.create_user(username='r', email='r@t.local', password=PW, role='receptionist')
        self.assertEqual(self.client.delete(f'/api/admin/staff/{r.id}/').status_code, 200)
        self.assertFalse(User.objects.filter(id=r.id).exists())
        self.course()
        r = self.client.delete(f'/api/admin/staff/{self.coach_user.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('suspend', r.data['detail'])
        self.assertEqual(self.client.delete(f'/api/admin/staff/{self.admin.id}/').status_code, 400)

    def test_managers_cannot_remove_admins(self):
        mgr = User.objects.create_user(username='mg', email='mg@t.local', password=PW, role='manager')
        other = User.objects.create_user(username='a2', email='a2@t.local', password=PW, role='admin')
        self.as_(mgr)
        self.assertEqual(self.client.delete(f'/api/admin/staff/{other.id}/').status_code, 403)


class SkuFormatTests(Base):
    def setUp(self):
        super().setUp()
        self.as_(self.admin)
        self.protein = ProductCategory.objects.create(key='protein', name='Protein')
        self.gear = ProductCategory.objects.create(key='gear', name='Gear')

    def add(self, **body):
        return self.client.post('/api/admin/shop/products/', {'name': body.pop('name', 'Whey Gold'), 'price': 99,
                                                              'category_id': self.protein.id, **body}, format='json')

    def test_prefix_follows_the_category_and_number_is_four_digits(self):
        self.assertEqual(self.add().data['sku'], 'PRO-0001')
        self.assertEqual(self.add(name='Casein', sku='7').data['sku'], 'PRO-0007')
        bad = self.add(name='Iso', sku='GEA-0002')
        self.assertIn('PRO-', bad.data['sku'])
        self.assertIn('PRO-0002', self.add(name='Iso', sku='PRO-0007').data['sku'])      # taken → next free shown
        self.assertEqual(self.client.get('/api/admin/shop/products/next-sku/', {'category_id': self.gear.id}).data,
                         {'prefix': 'GEA', 'sku': 'GEA-0001'})
        pid = Product.objects.get(sku='PRO-0001').id
        moved = self.client.patch(f'/api/admin/shop/products/{pid}/', {'category_id': self.gear.id}, format='json')
        self.assertEqual(moved.data['sku'], 'GEA-0001')

    def test_migration_fixes_old_skus(self):
        Product.objects.create(name='Old Whey', sku='whey-1kg', category=self.protein, price=10)
        Product.objects.create(name='Belt', sku='PRO-0001', category=self.gear, price=10)       # wrong prefix
        Product.objects.create(name='Good', sku='PRO-0003', category=self.protein, price=10)
        mig = importlib.import_module('apps.shop.migrations.0002_fixed_sku_format')
        mig.forward(django_apps, None)
        skus = dict(Product.objects.values_list('name', 'sku'))
        self.assertEqual(skus['Good'], 'PRO-0003')
        self.assertRegex(skus['Old Whey'], r'^PRO-\d{4}$')
        self.assertEqual(skus['Belt'], 'GEA-0001')


class InboxAndBadgesTests(Base):
    def test_new_admin_starts_clean(self):
        self.course()
        Enrollment.objects.create(member=self.member, course=Course.objects.first())
        self.as_(self.admin)
        self.assertEqual(set(self.client.get('/api/admin/activity/').data.values()), {0})


# ------------------------------------------------------------------ 2-step verification
class TwoFactorTests(Base):
    def login(self, email='a@t.local'):
        return self.client.post('/api/auth/login/', {'email': email, 'password': PW}, format='json')

    def test_admin_must_set_it_up_then_signs_in_with_app_codes(self):
        r = self.login()
        self.assertEqual(r.data['two_factor'], 'setup')
        self.assertNotIn('access', r.data)
        ticket = r.data['ticket']
        s = self.client.post('/api/auth/2fa/setup/method/', {'ticket': ticket, 'method': 'totp'}, format='json')
        self.assertTrue(s.data['otpauth_uri'].startswith('otpauth://totp/'))
        bad = self.client.post('/api/auth/2fa/setup/confirm/', {'ticket': ticket, 'code': '000000'}, format='json')
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post('/api/auth/2fa/setup/confirm/',
                              {'ticket': ticket, 'code': two_factor.totp_now(s.data['secret'])}, format='json')
        self.assertIn('access', ok.data)
        self.assertEqual(len(ok.data['backup_codes']), 8)
        backup = ok.data['backup_codes'][0]

        r = self.login()
        self.assertEqual((r.data['two_factor'], r.data['method']), ('verify', 'totp'))
        code = two_factor.totp_now(s.data['secret'])
        self.assertIn('access', self.client.post('/api/auth/2fa/verify/', {'ticket': r.data['ticket'], 'code': code},
                                                 format='json').data)
        r = self.login()           # the same code can't be used twice
        self.assertEqual(self.client.post('/api/auth/2fa/verify/', {'ticket': r.data['ticket'], 'code': code},
                                          format='json').status_code, 400)
        self.assertIn('access', self.client.post('/api/auth/2fa/verify/', {'ticket': r.data['ticket'], 'code': backup},
                                                 format='json').data)
        r = self.login()           # a backup code works once
        self.assertEqual(self.client.post('/api/auth/2fa/verify/', {'ticket': r.data['ticket'], 'code': backup},
                                          format='json').status_code, 400)

    def test_wrong_codes_end_the_ticket(self):
        TwoFactor.objects.create(user=self.admin, method='totp', secret=two_factor.new_secret(), enabled=True)
        ticket = self.login().data['ticket']
        for _ in range(4):
            self.client.post('/api/auth/2fa/verify/', {'ticket': ticket, 'code': '111111'}, format='json')
        r = self.client.post('/api/auth/2fa/verify/', {'ticket': ticket, 'code': '111111'}, format='json')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.post('/api/auth/2fa/verify/', {'ticket': 'made-up', 'code': '1'}, format='json')
                         .status_code, 401)

    def test_email_codes_and_members_choose(self):
        r = self.login('m@t.local')
        self.assertIn('access', r.data)                       # members: off unless they turn it on
        self.as_(self.member)
        self.assertEqual(self.client.post('/api/auth/2fa/enable/', {'password': 'nope', 'method': 'email'},
                                          format='json').status_code, 400)
        start = self.client.post('/api/auth/2fa/enable/', {'password': PW, 'method': 'email'}, format='json')
        code = mail.outbox[-1].subject.split(':')[1].split()[0]
        on = self.client.post('/api/auth/2fa/enable/', {'ticket': start.data['ticket'], 'code': code}, format='json')
        self.assertTrue(on.data['enabled'])
        self.client.force_authenticate(None)
        r = self.login('m@t.local')
        self.assertEqual((r.data['two_factor'], r.data['method']), ('verify', 'email'))
        code = mail.outbox[-1].subject.split(':')[1].split()[0]
        self.assertIn('access', self.client.post('/api/auth/2fa/verify/', {'ticket': r.data['ticket'], 'code': code},
                                                 format='json').data)
        # turning it off needs the password and a current code
        self.as_(self.member)
        self.client.post('/api/auth/2fa/send-code/')
        code = mail.outbox[-1].subject.split(':')[1].split()[0]
        self.assertEqual(self.client.post('/api/auth/2fa/disable/', {'password': PW, 'code': '123'}, format='json')
                         .status_code, 400)
        off = self.client.post('/api/auth/2fa/disable/', {'password': PW, 'code': code}, format='json')
        self.assertFalse(off.data['enabled'])

    def test_staff_cannot_turn_it_off_and_admins_can_reset_it(self):
        TwoFactor.objects.create(user=self.coach_user, method='totp', secret=two_factor.new_secret(), enabled=True)
        self.as_(self.coach_user)
        self.assertEqual(self.client.post('/api/auth/2fa/disable/', {'password': PW, 'code': '1'}, format='json')
                         .status_code, 400)
        self.as_(self.admin)
        r = self.client.post(f'/api/admin/staff/{self.coach_user.id}/reset-2fa/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(TwoFactor.objects.filter(user=self.coach_user).exists())
        self.client.force_authenticate(None)
        self.assertEqual(self.login('c@t.local').data['two_factor'], 'setup')     # sets it up again


# ------------------------------------------------------------------ coach portal
@override_settings(LOCKER_CREDIT_ENABLED=True)   # tests the locker credit feature itself
class CoachPortalTests(Base):
    def test_only_coaches_get_in(self):
        self.as_(self.member)
        self.assertEqual(self.client.get('/api/coach/me/').status_code, 403)
        self.as_(self.coach_user)
        self.assertEqual(self.client.get('/api/admin/members/').status_code, 403)
        me = self.client.get('/api/coach/me/').data
        self.assertEqual(me['user_email'], 'c@t.local')

    def test_profile_and_hours_but_not_the_price(self):
        self.as_(self.coach_user)
        r = self.client.patch('/api/coach/me/', {'bio': 'Strength coach.', 'hourly_rate': 1, 'is_verified': True},
                              format='json')
        self.assertEqual((r.data['bio'], r.data['hourly_rate'], r.data['is_verified']), ('Strength coach.', 100.0, False))
        h = self.client.put('/api/coach/hours/', {'hourly_rate': 1, 'windows': [
            {'day_of_week': 0, 'start_time': '09:00', 'end_time': '12:00'}]}, format='json')
        self.assertEqual((h.data['hourly_rate'], len(h.data['windows'])), ('100.00', 1))

    def test_course_goes_through_review(self):
        self.as_(self.coach_user)
        r = self.client.post('/api/coach/courses/', {'title': 'Kettlebell Basics', 'description': 'Swings and more.',
                                                     'category': 'Strength', 'price': 120, 'status': 'published',
                                                     'modules': [{'title': 'Day 1'}]}, format='json')
        self.assertEqual((r.status_code, r.data['status']), (201, 'draft'))
        cid = r.data['id']
        self.assertEqual(self.client.post(f'/api/coach/courses/{cid}/submit/').data['status'], 'pending')
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title='Course waiting for review').exists())
        self.as_(self.admin)
        self.assertEqual(self.client.patch(f'/api/admin/courses/{cid}/', {'status': 'draft'}, format='json').status_code, 400)
        self.client.patch(f'/api/admin/courses/{cid}/', {'status': 'draft', 'review_note': 'Add a warm-up.'}, format='json')
        self.assertTrue(Notification.objects.filter(recipient=self.coach_user, title='Changes requested on your course').exists())
        self.as_(self.coach_user)
        self.client.post(f'/api/coach/courses/{cid}/submit/')
        self.as_(self.admin)
        self.client.patch(f'/api/admin/courses/{cid}/', {'status': 'published'}, format='json')
        self.assertTrue(Notification.objects.filter(recipient=self.coach_user, title='Your course is live').exists())
        self.as_(self.coach_user)
        locked = self.client.patch(f'/api/coach/courses/{cid}/', {'price': 1}, format='json')
        self.assertEqual(locked.status_code, 400)
        self.assertEqual(self.client.patch(f'/api/coach/courses/{cid}/', {'subtitle': 'Swing well'}, format='json').status_code, 200)
        other = TrainerProfile.objects.create(user=User.objects.create_user(username='o', email='o@t.local', role='trainer'),
                                              title='Coach')
        foreign = Course.objects.create(trainer=other, title='Other', description='x', category='Strength')
        self.assertEqual(self.client.get(f'/api/coach/courses/{foreign.id}/').status_code, 404)

    def test_sessions_students_and_enrolment_notices(self):
        c = self.course()
        self.as_(self.member)
        self.assertEqual(self.client.post('/api/catalog/enrollments/', {'course_id': c.id}, format='json').status_code, 201)
        self.assertTrue(Notification.objects.filter(recipient=self.coach_user, title='A member started your course').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.admin, title='Course enrolment').exists())
        past = TrainerSession.objects.create(member=self.member, trainer=self.coach, status='scheduled',
                                             scheduled_at=timezone.now() - timedelta(hours=2), amount_charged=100)
        future = TrainerSession.objects.create(member=self.member, trainer=self.coach, status='scheduled',
                                               scheduled_at=timezone.now() + timedelta(days=3), amount_charged=100)
        self.as_(self.coach_user)
        self.assertEqual(self.client.post(f'/api/coach/sessions/{future.id}/status/', {'status': 'completed'},
                                          format='json').status_code, 400)
        self.assertEqual(self.client.post(f'/api/coach/sessions/{past.id}/status/', {'status': 'completed'},
                                          format='json').data['status'], 'completed')
        self.client.post(f'/api/coach/sessions/{future.id}/status/', {'status': 'cancelled'}, format='json')
        self.assertEqual(LockerCredit.objects.get(member=self.member).balance, Decimal('100'))
        st = self.client.get('/api/coach/students/').data
        self.assertEqual((st['enrolments'][0]['name'], st['clients'][0]['completed']), ('Sara Ali', 1))
        self.assertEqual(self.client.get('/api/coach/dashboard/').status_code, 200)
        self.assertEqual(self.client.get('/api/coach/earnings/').data['all_time']['sessions'], 100.0)

    def test_paid_course_notifies_the_gym_with_the_amount(self):
        c = self.course(price=Decimal('80'))
        self.as_(self.member)
        self.credit(100)
        self.assertIn(self.pay({'purpose': 'course', 'course_id': c.id}).status_code, (200, 201))
        n = Notification.objects.get(recipient=self.admin, title='Course purchased')
        self.assertIn('SAR 80', n.message)

    def test_staff_invite_gives_coaches_a_profile_and_coach_link(self):
        self.as_(self.admin)
        r = self.client.post('/api/admin/staff/', {'email': 'k@t.local', 'first_name': 'Khalid', 'last_name': 'Omar',
                                                  'role': 'trainer'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(TrainerProfile.objects.filter(user__email='k@t.local').exists())
        self.assertIn('/coach/login', mail.outbox[-1].body)
        t = TrainerProfile.objects.create(user=User.objects.create_user(username='z', email='z@t.local', role='member'),
                                          title='Coach')
        p = self.client.post(f'/api/admin/trainers/{t.id}/portal-access/', {'password': 'Another-Pass-9'}, format='json')
        self.assertEqual(p.status_code, 200, p.data)
        t.user.refresh_from_db()
        self.assertEqual((t.user.role, t.user.must_change_password), ('trainer', True))


# ------------------------------------------------------------------ machine videos
@override_settings(LOCKER_CREDIT_ENABLED=True)   # tests the locker credit feature itself
class VideoTierTests(Base):
    def setUp(self):
        super().setUp()
        self.ex = Exercise.objects.create(name='Chest Press', category='strength', primary_muscle='chest')

    def test_coach_adds_videos_and_members_see_locks(self):
        self.as_(self.coach_user)
        bad = self.client.post('/api/coach/videos/', {'exercise_id': self.ex.id, 'title': 'Pro tips', 'tier': 'premium',
                                                      'video_url': 'https://youtu.be/x', 'price': 0}, format='json')
        self.assertIn('price', bad.data)
        for tier, price in (('basic', 50), ('advanced', 15), ('premium', 30)):
            r = self.client.post('/api/coach/videos/', {'exercise_id': self.ex.id, 'title': f'{tier} video',
                                                        'tier': tier, 'video_url': f'https://youtu.be/{tier}',
                                                        'price': price}, format='json')
            self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(ExerciseVideo.objects.get(tier='basic').price, 0)        # basic is always free
        self.as_(self.member)
        rows = {v['tier']: v for v in self.client.get(f'/api/workouts/exercises/{self.ex.id}/videos/').data['videos']}
        self.assertEqual((rows['basic']['locked'], rows['advanced']['locked'], rows['premium']['locked']), (False, True, True))
        self.assertIsNone(rows['premium']['video_url'])                           # never leaked while locked

        # buy one video → unlocked for good; the coach hears about it
        self.credit(50)
        self.assertIn(self.pay({'purpose': 'video', 'video_id': rows['advanced']['id']}).status_code, (200, 201))
        rows = {v['tier']: v for v in self.client.get(f'/api/workouts/exercises/{self.ex.id}/videos/').data['videos']}
        self.assertEqual((rows['advanced']['locked'], rows['advanced']['access']), (False, 'bought'))
        self.assertTrue(Notification.objects.filter(recipient=self.coach_user, title='Someone bought your video').exists())
        self.assertEqual(self.pay({'purpose': 'video', 'video_id': rows['advanced']['id']}).status_code, 400)

        # a plan that includes premium videos unlocks the rest without paying
        self.plan.video_access = 'premium'
        self.plan.save()
        rows = {v['tier']: v for v in self.client.get(f'/api/workouts/exercises/{self.ex.id}/videos/').data['videos']}
        self.assertEqual((rows['premium']['locked'], rows['premium']['access']), (False, 'plan'))
        self.assertEqual(self.pay({'purpose': 'video', 'video_id': rows['premium']['id']}).status_code, 400)

    def test_bought_videos_are_hidden_not_deleted(self):
        v = ExerciseVideo.objects.create(exercise=self.ex, coach=self.coach, tier='advanced', title='Adv',
                                         video_url='https://youtu.be/a', price=10)
        VideoPurchase.objects.create(member=self.member, video=v, amount=10)
        self.as_(self.coach_user)
        self.assertEqual(self.client.delete(f'/api/coach/videos/{v.id}/').status_code, 400)
        self.assertEqual(self.client.patch(f'/api/coach/videos/{v.id}/', {'tier': 'basic'}, format='json').status_code, 400)
        self.as_(self.admin)
        self.assertFalse(self.client.patch(f'/api/admin/machine-videos/{v.id}/', {'is_active': False},
                                           format='json').data['is_active'])


# ------------------------------------------------------------------ paid classes
@override_settings(LOCKER_CREDIT_ENABLED=True)   # tests the locker credit feature itself
class PaidClassTests(Base):
    def make(self, **kw):
        nxt = timezone.localdate() + timedelta(days=2)
        return GymClass.objects.create(name='Spin Pro', category='endurance', day_of_week=nxt.weekday(),
                                       start_time=time(18, 0), capacity=kw.pop('capacity', 2), trainer=self.coach, **kw)

    def test_paid_class_is_locked_until_paid_per_session(self):
        c = self.make(price_per_session=Decimal('25'))
        self.as_(self.member)
        row = next(x for x in self.client.get('/api/classes/').data['classes'] if x['id'] == c.id)
        self.assertTrue(row['locked'])
        self.assertTrue(row['session_dates'])
        self.assertEqual(self.client.post(f'/api/classes/{c.id}/book/').status_code, 402)
        day = row['session_dates'][0]['date']
        self.credit(100)
        r = self.pay({'purpose': 'class_pass', 'class_id': c.id, 'kind': 'session', 'date': str(day)})
        self.assertIn(r.status_code, (200, 201), r.data)
        row = next(x for x in self.client.get('/api/classes/').data['classes'] if x['id'] == c.id)
        self.assertFalse(row['locked'])
        self.assertEqual(row['my_access']['sessions'], [day])
        self.assertEqual(self.pay({'purpose': 'class_pass', 'class_id': c.id, 'kind': 'session',
                                   'date': str(day)}).status_code, 400)            # already paid
        self.assertEqual(self.pay({'purpose': 'class_pass', 'class_id': c.id, 'kind': 'full'}).status_code, 400)  # no end date
        # cancelling 24h+ ahead refunds the session
        done = self.client.post(f'/api/classes/{c.id}/cancel/', {'date': str(day)}, format='json')
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(LockerCredit.objects.get(member=self.member).balance, Decimal('100'))

    def test_whole_class_and_capacity_per_date(self):
        c = self.make(price_per_session=Decimal('20'), full_price=Decimal('60'), capacity=1,
                      starts_on=timezone.localdate(), ends_on=timezone.localdate() + timedelta(days=20))
        other = User.objects.create_user(username='o2', email='o2@t.local', password=PW, role='member')
        Membership.objects.create(member=other, plan=self.plan, start_date=timezone.localdate(),
                                  expiry_date=timezone.localdate() + timedelta(days=60), iqama_number='2111111112')
        self.as_(self.member)
        self.credit(100)
        r = self.pay({'purpose': 'class_pass', 'class_id': c.id, 'kind': 'full'})
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(LockerCredit.objects.get(member=self.member).balance, Decimal('40'))
        self.assertTrue(ClassEnrollment.objects.filter(gym_class=c, member=self.member, status='enrolled').exists())
        self.assertTrue(Notification.objects.filter(recipient=self.coach_user, title='New class booking').exists())
        # the only spot is taken on every date → the other member can't buy
        self.client.force_authenticate(other)
        LockerCredit.objects.create(member=other, balance=Decimal('100'))
        row = next(x for x in self.client.get('/api/classes/').data['classes'] if x['id'] == c.id)
        self.assertEqual(row['session_dates'][0]['spots_left'], 0)
        r = self.client.post('/api/payments/checkout/', {'purpose': 'class_pass', 'class_id': c.id, 'kind': 'session',
                                                         'date': str(row['session_dates'][0]['date']),
                                                         'method': 'locker_credit'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('full', r.data['detail'])
        # paid history protects the class
        self.as_(self.admin)
        self.assertEqual(self.client.delete(f'/api/classes/admin/{c.id}/').status_code, 400)
        self.assertEqual(ClassPass.objects.filter(gym_class=c).count(), 1)

    def test_admin_sets_prices_and_free_classes_still_book(self):
        self.as_(self.admin)
        bad = self.client.post('/api/classes/admin/', {'name': 'Boxing Pro', 'category': 'combat', 'day_of_week': 1,
                                                       'start_time': '19:00', 'full_price': 100}, format='json')
        self.assertIn('ends_on', bad.data)
        ok = self.client.post('/api/classes/admin/', {'name': 'Boxing Pro', 'category': 'combat', 'day_of_week': 1,
                                                      'start_time': '19:00', 'price_per_session': 30,
                                                      'starts_on': str(timezone.localdate()),
                                                      'ends_on': str(timezone.localdate() + timedelta(days=30))}, format='json')
        self.assertEqual((ok.status_code, ok.data['is_paid']), (201, True))
        free = self.make()
        self.as_(self.member)
        self.assertEqual(self.client.post(f'/api/classes/{free.id}/book/').data['status'], 'enrolled')
        self.as_(self.coach_user)
        mine = self.client.get('/api/coach/classes/').data
        self.assertEqual(mine[0]['roster'][0]['name'], 'Sara Ali')
