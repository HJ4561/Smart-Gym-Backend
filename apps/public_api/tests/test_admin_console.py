"""Admin console: alerts, search, revenue figures, facilities hub, member management."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.members.models import CheckIn, FacilityAddon, Membership, MembershipPlan, MembershipRegistration as R
from apps.notifications.models import Notification
from apps.payments.gateway import record_transaction
from apps.payments.models import Transaction
from apps.site_content.models import SiteItem

User = get_user_model()


class ConsoleBase(APITestCase):
    def setUp(self):
        self.plan = MembershipPlan.objects.create(name='pro', display_name='Pro', price_monthly=79, price_annual=790)
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')
        self.manager = User.objects.create_user(username='mg', email='mg@t.local', password='x', role='manager')
        self.member = User.objects.create_user(username='m', email='sara@t.local', password='x', role='member',
                                               first_name='Sara', last_name='Ali')
        today = date.today()
        self.ms = Membership.objects.create(member=self.member, plan=self.plan, status='active',
                                            start_date=today, expiry_date=today + timedelta(days=60),
                                            iqama_number='2111111111')
        self.client.force_authenticate(self.admin)

    def act(self, **body):
        return self.client.post(f'/api/admin/members/{self.member.id}/action/', body, format='json')


class AlertsSearchRevenueTests(ConsoleBase):
    def test_alerts_count_real_work(self):
        self.assertEqual(self.client.get('/api/admin/alerts/').data['total'], 0)
        reg = R.objects.create(iqama_number='2999999999', full_name='New Person', email='n@t.local', phone='1', age=30,
                               gender='male', plan=self.plan, duration_months=1, status=R.STATUS_PAID, payment_status='paid')
        data = self.client.get('/api/admin/alerts/').data
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['path'], f'/admin/registrations?highlight={reg.id}')

    def test_search_finds_member_by_iqama(self):
        res = self.client.get('/api/admin/search/', {'q': '2111111111'}).data
        self.assertEqual(res['members'][0]['title'], 'Sara Ali')
        self.assertIn('iqama=2111111111', res['members'][0]['path'])

    def test_members_cannot_use_console(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get('/api/admin/alerts/').status_code, 403)

    def test_revenue_counts_cash_once(self):
        record_transaction(self.member, 'Locker credit top-up', 'topup', Decimal('100'), 'MADA •••• 1', 'p1')
        record_transaction(self.member, 'Shop order', 'retail', Decimal('40'), 'Locker credit', 'c1')   # spends the top-up
        record_transaction(self.member, 'Membership', 'subscription', Decimal('237'), 'MADA •••• 1', 'p2')
        ov = self.client.get('/api/admin/overview/', {'range': '7d'}).data
        self.assertEqual(ov['revenue']['last_30d'], 337.0)
        self.assertEqual(ov['revenue']['range_label'], 'Last 7 days')
        fr = self.client.get('/api/admin/flow-and-revenue/').data
        self.assertEqual({s['label'] for s in fr['revenue_streams']}, {'Memberships & add-ons', 'Locker credit top-ups'})
        csv_body = self.client.get('/api/admin/finance/transactions/export/').content.decode('utf-8-sig')
        self.assertEqual(csv_body.count('\n'), 4)   # header + 3 rows

    def test_empty_revenue_is_zero_not_one(self):
        self.assertEqual(self.client.get('/api/admin/flow-and-revenue/').data['summary']['total_30d'], 0)


class FacilitiesHubTests(ConsoleBase):
    def test_status_visibility_capacity(self):
        item = SiteItem.objects.create(collection='facilities', data={'title': {'en': 'Pool'}, 'status_tone': 'ok'})
        before = self.client.get('/api/admin/facility-pricing/').data['summary']['on_website']
        r = self.client.patch(f'/api/admin/facility-pricing/{item.id}/',
                              {'status_tone': 'closed', 'is_active': False, 'capacity': 25}, format='json')
        self.assertEqual((r.data['status_tone'], r.data['status_text'], r.data['is_active'], r.data['capacity']),
                         ('closed', 'CLOSED', False, 25))
        item.refresh_from_db()
        self.assertEqual(item.data['status']['ar'], 'مغلق')
        self.assertEqual(self.client.get('/api/admin/facility-pricing/').data['summary']['on_website'], before - 1)

    def test_capacity_feeds_occupancy_and_nodes(self):
        item = SiteItem.objects.create(collection='facilities', data={'title': {'en': 'Pool'}})
        FacilityAddon.objects.create(item=item, capacity=10)
        CheckIn.objects.create(member=self.member, zone='Pool')
        occ = self.client.get('/api/dashboard/occupancy/').data
        self.assertEqual((occ['capacity'], occ['percent']), (10, 10))
        nodes = self.client.get('/api/admin/facility-nodes/').data
        self.assertEqual((nodes[0]['name'], nodes[0]['active_now'], nodes[0]['utilization']), ('Pool', 1, 10.0))


class MemberOpsTests(ConsoleBase):
    def test_list_shows_membership_state_and_filters(self):
        rows = self.client.get('/api/admin/members/').data['results']
        self.assertEqual((rows[0]['plan'], rows[0]['membership_status']), ('Pro', 'active'))
        self.assertEqual(self.client.get('/api/admin/members/', {'status': 'expired'}).data['count'], 0)

    def test_extend_with_cash_records_payment(self):
        old = self.ms.expiry_date
        self.assertEqual(self.act(action='extend', months=3, payment='cash').status_code, 400)   # amount needed
        r = self.act(action='extend', months=3, payment='cash', amount='237')
        self.assertEqual(r.status_code, 200)
        self.ms.refresh_from_db()
        from dateutil.relativedelta import relativedelta
        self.assertEqual(self.ms.expiry_date, old + relativedelta(months=3))
        self.assertEqual(Transaction.objects.get(member=self.member).payment_method_label, 'Cash at desk')

    def test_complimentary_extension_records_no_payment(self):
        self.act(action='extend', months=1, payment='complimentary')
        self.assertFalse(Transaction.objects.exists())

    def test_freeze_keeps_paid_time_and_unfreeze_returns_unused(self):
        old = self.ms.expiry_date
        self.act(action='freeze', days=10)
        self.ms.refresh_from_db()
        self.assertTrue(self.ms.effectively_frozen)
        self.assertFalse(self.ms.allows_entry)
        self.assertEqual(self.ms.expiry_date, old + timedelta(days=10))
        self.act(action='unfreeze')
        self.ms.refresh_from_db()
        self.assertEqual(self.ms.expiry_date, old)          # unfrozen same day: all 10 days returned
        self.assertTrue(self.ms.allows_entry)

    def test_freeze_ends_by_itself(self):
        self.ms.is_frozen, self.ms.status, self.ms.frozen_until = True, 'frozen', date.today()
        self.ms.save()
        self.assertTrue(self.ms.allows_entry)

    def test_message_becomes_notification(self):
        self.act(action='message', title='Pool closed', message='Maintenance on Friday.')
        self.assertTrue(Notification.objects.filter(recipient=self.member, title='Pool closed').exists())

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_reset_password_emails_temp_and_locks(self):
        self.assertEqual(self.act(action='reset_password').status_code, 200)
        self.member.refresh_from_db()
        self.assertTrue(self.member.must_change_password)
        self.assertIn('Temporary password', mail.outbox[0].body)

    def test_managers_cannot_change_roles(self):
        self.client.force_authenticate(self.manager)
        r = self.client.patch(f'/api/admin/members/{self.member.id}/update/', {'role': 'admin'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_bulk_export_csv(self):
        r = self.client.post('/api/admin/members/bulk-action/', {'action': 'export', 'member_ids': [self.member.id]},
                             format='json')
        self.assertIn('2111111111', r.content.decode('utf-8-sig'))


class FinanceTests(ConsoleBase):
    def test_finance_figures_from_transactions(self):
        record_transaction(self.member, 'Locker credit top-up', 'topup', Decimal('100'), 'MADA •••• 1', 'p1')
        record_transaction(self.member, 'Shop order', 'retail', Decimal('40'), 'Locker credit', 'c1')
        record_transaction(self.member, 'Refund — order', 'refund', Decimal('-20'), 'MADA •••• 1', 'r1')
        record_transaction(self.member, 'Membership extension', 'subscription', Decimal('79'), 'Cash at desk', 'd1')
        ov = self.client.get('/api/admin/finance/overview/').data
        self.assertEqual((ov['received'], ov['refunds']), (159.0, 20.0))
        streams = self.client.get('/api/admin/finance/revenue-streams/').data
        self.assertEqual({m['label'] for m in streams['payment_methods']}, {'mada', 'Cash at desk'})
        months = self.client.get('/api/admin/finance/revenue-monthly/', {'months': 3}).data
        self.assertEqual((len(months), months[-1]['revenue']), (3, 159.0))
        self.assertNotIn('target', months[-1])
        top = self.client.get('/api/admin/finance/top-members/').data['members']
        self.assertEqual((top[0]['name'], top[0]['ltv']), ('Sara Ali', 159.0))
        t = Transaction.objects.get(gateway_reference='d1')
        detail = self.client.get(f'/api/admin/finance/transactions/{t.id}/').data
        self.assertEqual((detail['method'], detail['invoice']['status']), ('Cash at desk', 'paid'))
        self.assertEqual(self.client.get('/api/admin/finance/transactions/', {'type': 'refund'}).data['total'], 1)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_email_receipt(self):
        t = record_transaction(self.member, 'Membership', 'subscription', Decimal('79'), 'MADA •••• 1', 'p9')
        self.assertEqual(self.client.post(f'/api/admin/finance/transactions/{t.id}/email-receipt/').status_code, 200)
        self.assertIn('SAR 79.00', mail.outbox[0].body)


class RejectRefundTests(ConsoleBase):
    def paid_reg(self):
        return R.objects.create(iqama_number='2999999999', full_name='New Person', email='n@t.local', phone='1', age=30,
                                gender='male', plan=self.plan, duration_months=1, status=R.STATUS_PAID,
                                payment_status='paid', payment_amount=Decimal('99.00'), moyasar_payment_id='pay_1')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_reject_with_refund_refunds_full_amount_and_emails(self):
        reg = self.paid_reg()
        with patch('apps.public_api.moyasar.refund_payment') as refund:
            r = self.client.post(f'/api/admin/registrations-api/registrations/{reg.id}/reject/',
                                 {'reason': 'Iqama photo unclear', 'refund': True}, format='json')
        self.assertEqual((r.status_code, r.data['refunded']), (200, True))
        refund.assert_called_once_with('pay_1', 9900)
        reg.refresh_from_db()
        self.assertEqual((reg.status, reg.payment_status), (R.STATUS_REJECTED, 'refunded'))
        self.assertIn('SAR 99.00 has been refunded', mail.outbox[0].body)

    def test_failed_refund_leaves_signup_untouched(self):
        from apps.public_api.moyasar import MoyasarError
        reg = self.paid_reg()
        with patch('apps.public_api.moyasar.refund_payment', side_effect=MoyasarError('already refunded')):
            r = self.client.post(f'/api/admin/registrations-api/registrations/{reg.id}/reject/',
                                 {'refund': True}, format='json')
        self.assertEqual(r.status_code, 400)
        reg.refresh_from_db()
        self.assertEqual((reg.status, reg.payment_status), (R.STATUS_PAID, 'paid'))

    def test_reject_without_refund_keeps_payment(self):
        reg = self.paid_reg()
        with patch('apps.public_api.moyasar.refund_payment') as refund:
            self.client.post(f'/api/admin/registrations-api/registrations/{reg.id}/reject/', {}, format='json')
        refund.assert_not_called()
        reg.refresh_from_db()
        self.assertEqual(reg.payment_status, 'paid')


class TrainersCoursesTests(ConsoleBase):
    def add_trainer(self, **extra):
        body = {'email': 'coach@t.local', 'first_name': 'Omar', 'last_name': 'Saleh', 'title': 'Strength coach',
                'hourly_rate': '150', 'specialties': 'Powerlifting, Mobility', **extra}
        return self.client.post('/api/admin/trainers/', body, format='json')

    def test_add_edit_verify_and_hide_trainer(self):
        r = self.add_trainer()
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.data['user_name'], r.data['specialties'], r.data['hourly_rate']),
                         ('Omar Saleh', ['Powerlifting', 'Mobility'], 150.0))
        u = User.objects.get(email='coach@t.local')
        self.assertEqual(u.role, 'trainer')
        self.assertFalse(u.has_usable_password())
        self.assertEqual(self.add_trainer().status_code, 400)           # already a trainer
        tid = r.data['id']
        self.client.patch(f'/api/admin/trainers/{tid}/', {'is_verified': True}, format='json')
        self.assertEqual(self.client.get('/api/admin/trainers/stats/').data['verified'], 1)
        self.client.patch(f'/api/admin/trainers/{tid}/', {'is_active': False}, format='json')
        self.assertEqual(self.client.get('/api/catalog/trainers/').data, [])   # hidden from members
        self.assertEqual(len(self.client.get('/api/admin/trainers/', {'status': 'hidden'}).data), 1)

    def test_existing_member_becomes_trainer_and_bad_rate_rejected(self):
        self.assertEqual(self.add_trainer(email='sara@t.local', hourly_rate='-5').status_code, 400)
        r = self.add_trainer(email='SARA@t.local')
        self.assertEqual(r.data['user_id'], self.member.id)

    def test_course_lifecycle(self):
        tid = self.add_trainer().data['id']
        r = self.client.post('/api/admin/courses/', {'title': '8-week strength', 'description': 'Build a base.',
                                                     'category': 'Strength', 'trainer_id': tid, 'price': '199'},
                             format='json')
        self.assertEqual((r.status_code, r.data['status']), (201, 'draft'))
        cid = r.data['id']
        self.assertNotIn('8-week strength', str(self.client.get('/api/catalog/courses/').data))   # drafts stay hidden
        bad = self.client.patch(f'/api/admin/courses/{cid}/', {'status': 'published'}, format='json')
        self.assertIn('modules', bad.data)                                  # needs a lesson first
        ok = self.client.patch(f'/api/admin/courses/{cid}/', {'status': 'published', 'modules': [
            {'title': 'Squat basics', 'week_number': 1, 'day_number': 1, 'duration_minutes': 45}]}, format='json')
        self.assertEqual((ok.status_code, ok.data['module_count']), (200, 1))
        mid = ok.data['modules'][0]['id']
        again = self.client.patch(f'/api/admin/courses/{cid}/', {'modules': [
            {'id': mid, 'title': 'Squat basics v2'}, {'title': 'Bench basics'}]}, format='json')
        self.assertEqual([m['id'] == mid for m in again.data['modules']], [True, False])   # kept its id
        self.assertEqual(self.client.get('/api/admin/courses/stats/').data['published'], 1)
        from apps.catalog.models import Course, Enrollment
        Enrollment.objects.create(member=self.member, course=Course.objects.get(id=cid))
        self.assertEqual(self.client.delete(f'/api/admin/courses/{cid}/').status_code, 400)

    def test_managers_and_members(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get('/api/admin/courses/').status_code, 403)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class StaffTests(ConsoleBase):
    def add(self, **extra):
        body = {'email': 'desk@t.local', 'first_name': 'Lina', 'role': 'receptionist', **extra}
        return self.client.post('/api/admin/staff/', body, format='json')

    def test_add_receptionist_no_login_and_manager_gets_invite(self):
        r = self.add(shift_start='06:00', shift_end='14:00')
        self.assertEqual((r.status_code, r.data['can_sign_in'], r.data['shift_display']), (201, False, '06:00 - 14:00'))
        self.assertEqual(len(mail.outbox), 0)
        r = self.add(email='boss@t.local', first_name='Huda', role='manager')
        self.assertEqual((r.status_code, r.data['can_sign_in']), (201, True))
        self.assertIn('/admin/login', mail.outbox[0].body)
        self.assertTrue(User.objects.get(email='boss@t.local').must_change_password)

    def test_duplicate_name_and_bad_email_are_refused(self):
        self.assertEqual(self.add().status_code, 201)
        r = self.add(email='other@t.local')                       # same name "Lina"
        self.assertEqual(r.status_code, 400)
        self.assertIn('already on the team', r.data['first_name'])
        r = self.add(email='bad@@mail', first_name='Sami')
        self.assertIn('valid email', r.data['email'])
        r = self.add(email='p@t.local', first_name='Sami', phone='05abc')
        self.assertIn('Saudi mobile', r.data['phone'])

    def test_admin_can_set_a_starting_password(self):
        r = self.add(email='boss2@t.local', first_name='Omar', role='admin', password='Start-Pass-123')
        self.assertEqual(r.status_code, 201)
        u = User.objects.get(email='boss2@t.local')
        self.assertTrue(u.check_password('Start-Pass-123') and u.must_change_password)
        self.assertEqual(len(mail.outbox), 0)

    def test_managers_cannot_create_or_edit_admins(self):
        self.client.force_authenticate(self.manager)
        self.assertEqual(self.add(role='admin').status_code, 403)
        self.assertEqual(self.client.patch(f'/api/admin/staff/{self.admin.id}/', {'status': 'suspended'},
                                           format='json').status_code, 403)
        self.assertEqual(self.add().status_code, 201)

    def test_cannot_suspend_self_or_last_admin(self):
        r = self.client.patch(f'/api/admin/staff/{self.admin.id}/', {'status': 'suspended'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_suspend_blocks_login_and_stats_are_real(self):
        uid = self.add(shift_start='00:00', shift_end='23:59', joined_at='2024-01-01').data['id']
        stats = self.client.get('/api/admin/staff/stats/').data
        self.assertEqual(stats['on_shift_now'], 1)
        self.assertGreater(stats['avg_tenure_years'], 0)
        self.assertEqual(self.add(shift_start='09:00').status_code, 400)        # end missing / duplicate email
        self.client.patch(f'/api/admin/staff/{uid}/', {'status': 'suspended'}, format='json')
        self.assertFalse(User.objects.get(id=uid).is_active)
        self.assertEqual(self.client.get('/api/admin/staff/stats/').data['suspended'], 1)


class MachinesTests(ConsoleBase):
    def test_add_edit_offline_and_regenerate(self):
        r = self.client.post('/api/admin/machines/', {'name': 'Leg Press', 'category': 'strength',
                                                      'primary_muscle': 'legs', 'technique_cues': 'Feet flat\nKnees out'},
                             format='json')
        self.assertEqual(r.status_code, 201)
        tag, mid = r.data['qr_tag_id'], r.data['id']
        self.assertTrue(tag.startswith('SG-MCH-'))
        self.assertEqual(r.data['technique_cues'], ['Feet flat', 'Knees out'])
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(f'/api/workouts/exercises/qr/{tag.lower()}/').data['name'], 'Leg Press')
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post('/api/admin/machines/', {'name': 'leg press', 'category': 'strength',
                                                                   'primary_muscle': 'legs'}, format='json').status_code, 400)
        self.client.patch(f'/api/admin/machines/{mid}/', {'is_active': False}, format='json')
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(f'/api/workouts/exercises/qr/{tag}/').status_code, 404)   # offline
        self.client.force_authenticate(self.admin)
        self.client.patch(f'/api/admin/machines/{mid}/', {'is_active': True}, format='json')
        new = self.client.post(f'/api/admin/machines/{mid}/regenerate-qr/').data['qr_tag_id']
        self.assertNotEqual(new, tag)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(f'/api/workouts/exercises/qr/{tag}/').status_code, 404)   # old tag dead
        self.client.force_authenticate(self.admin)
        offline = self.client.get('/api/admin/machines/', {'status': 'offline'}).data
        self.assertEqual(offline, [])
        self.assertIn('categories', self.client.get('/api/admin/machines/categories/').data)


class ClassesTests(ConsoleBase):
    def test_admin_creates_member_books_waitlist_promotes(self):
        r = self.client.post('/api/classes/admin/', {'name': 'Spin', 'category': 'endurance', 'day_of_week': 6,
                                                     'start_time': '18:30', 'capacity': 1}, format='json')
        self.assertEqual((r.status_code, r.data['day'], r.data['end_time']), (201, 'Sunday', '19:30'))
        cid = r.data['id']
        other = User.objects.create_user(username='o', email='o@t.local', password='x', role='member')
        Membership.objects.create(member=other, plan=self.plan, status='active', start_date=date.today(),
                                  expiry_date=date.today() + timedelta(days=30), iqama_number='2222222222')
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.post(f'/api/classes/{cid}/book/').data['status'], 'enrolled')
        self.assertEqual(self.client.post(f'/api/classes/{cid}/book/').status_code, 400)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.post(f'/api/classes/{cid}/book/').data['status'], 'waitlist')
        mine = self.client.get('/api/classes/').data['classes'][0]
        self.assertEqual((mine['my_status'], mine['my_waitlist_position']), ('waitlist', 1))
        self.client.force_authenticate(self.member)
        self.client.post(f'/api/classes/{cid}/cancel/')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get('/api/classes/').data['classes'][0]['my_status'], 'enrolled')   # promoted
        self.assertTrue(Notification.objects.filter(recipient=other, title='You’re in!').exists())
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get('/api/classes/stats/').data['fill_rate'], 100.0)
        self.assertEqual(self.client.patch(f'/api/classes/admin/{cid}/', {'capacity': 0}, format='json').status_code, 400)
        roster = self.client.get(f'/api/classes/admin/{cid}/roster/').data['roster']
        self.assertEqual([x['email'] for x in roster], ['o@t.local'])

    def test_needs_membership_and_admin_only(self):
        c = self.client.post('/api/classes/admin/', {'name': 'Yoga', 'category': 'mobility', 'day_of_week': 0,
                                                     'start_time': '07:00'}, format='json').data
        self.ms.status = 'expired'
        self.ms.save()
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.post(f"/api/classes/{c['id']}/book/").status_code, 403)
        self.assertEqual(self.client.get('/api/classes/admin/').status_code, 403)


class ReportsTests(ConsoleBase):
    def test_every_report_builds_and_downloads(self):
        record_transaction(self.member, 'Membership', 'subscription', Decimal('79'), 'MADA •••• 1', 'p1')
        record_transaction(self.member, 'Shop order', 'retail', Decimal('20'), 'Locker credit', 'c1')   # not new money
        CheckIn.objects.create(member=self.member, zone='Main floor')
        keys = [t['key'] for t in self.client.get('/api/reports/templates/').data]
        self.assertEqual(set(keys), {'revenue', 'finance', 'members', 'attendance', 'machines', 'trainers'})
        today = timezone.localdate().isoformat()        # gym time, like the reports
        for k in keys:
            r = self.client.post('/api/reports/generate/', {'template_key': k, 'start': '2025-01-01', 'end': today},
                                 format='json')
            self.assertEqual(r.status_code, 201, k)
        r = self.client.post('/api/reports/generate/', {'template_key': 'revenue', 'start': timezone.localdate().replace(day=1).isoformat(),
                                                        'end': today}, format='json')
        self.assertEqual((r.status_code, r.data['row_count'], r.data['format']), (201, 2, 'CSV'))   # 1 payment + total
        body = self.client.get(f"/api/reports/history/{r.data['report_id']}/download/").content.decode('utf-8-sig')
        self.assertIn('Total received · إجمالي المستلم,79.00', body)
        self.assertIn('Date (gym time) · التاريخ (توقيت النادي)', body)
        self.assertNotIn('Shop order', body)
        att = self.client.post('/api/reports/generate/', {'template_key': 'attendance', 'start': today, 'end': today},
                               format='json').data
        self.assertEqual(att['row_count'], 1)
        self.assertEqual(self.client.post('/api/reports/generate/', {'template_key': 'members', 'start': today,
                                                                     'end': '2020-01-01'}, format='json').status_code, 400)


class InsightsTests(ConsoleBase):
    def test_kpis_growth_and_insights_are_real(self):
        k = self.client.get('/api/admin/analytics/kpis/').data
        self.assertEqual((k['total_members'], k['active_members'], k['visit_rate']), (1, 1, 0.0))
        g = self.client.get('/api/admin/analytics/growth/').data
        self.assertEqual(len(g), 12)
        self.assertEqual(g[-1]['new_members'], 1)                     # latest month last, zeros filled
        self.ms.expiry_date = date.today() + timedelta(days=3)
        self.ms.save()
        items = self.client.get('/api/admin/analytics/insights/').data
        ending = next(i for i in items if i['icon'] == 'hourglass_bottom')
        self.assertEqual((ending['ids'], ending['path']), ([self.member.id], f'/admin/members?highlight={self.member.id}'))

    def test_members_list_can_be_narrowed_to_ids(self):
        other = User.objects.create_user(username='o@t.local', email='o@t.local', password='x', role='member')
        rows = self.client.get('/api/admin/members/', {'ids': f'{self.member.id},abc'}).data['results']
        self.assertEqual([r['id'] for r in rows], [self.member.id])
        self.assertEqual(self.client.get('/api/admin/members/').data['count'], 2)
        self.assertTrue(other.id)


class SystemStatusTests(ConsoleBase):
    @override_settings(MOYASAR_SECRET_KEY='sk_test_abc', MOYASAR_WEBHOOK_SECRET='')
    def test_reports_configuration_without_secrets(self):
        r = self.client.get('/api/admin/settings/')
        self.assertEqual((r.data['payments']['mode'], r.data['payments']['webhook_secret_set']), ('test', False))
        self.assertNotIn('sk_test_abc', str(r.data))
        self.assertEqual(r.data['me']['role'], 'admin')
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get('/api/admin/settings/').status_code, 403)


class ShopCatalogueTests(ConsoleBase):
    def test_categories_products_promos(self):
        cat = self.client.post('/api/admin/shop/categories/', {'name': 'Protein'}, format='json').data
        self.assertEqual(cat['key'], 'protein')
        r = self.client.post('/api/admin/shop/products/', {'name': 'Whey 1kg', 'category_id': cat['id'], 'price': '149',
                                                           'stock_quantity': 3}, format='json')
        self.assertEqual((r.status_code, r.data['sku'], r.data['is_low_stock']), (201, 'PRO-0001', True))   # SKU: category prefix + number
        pid = r.data['id']
        self.assertEqual(self.client.patch(f'/api/admin/shop/products/{pid}/', {'original_price': '100'},
                                           format='json').status_code, 400)          # "was" price must be higher
        self.client.force_authenticate(self.member)
        self.assertEqual([p['name'] for p in self.client.get('/api/shop/products/').data], ['Whey 1kg'])
        self.client.force_authenticate(self.admin)
        self.client.patch(f"/api/admin/shop/categories/{cat['id']}/", {'is_active': False}, format='json')
        self.assertEqual(self.client.get('/api/shop/products/').data, [])           # hidden category hides products
        self.assertEqual(self.client.delete(f"/api/admin/shop/categories/{cat['id']}/").status_code, 400)
        promo = self.client.post('/api/admin/shop/promos/', {'code': 'ramadan 20', 'discount_percent': 20,
                                                             'valid_until': '2099-01-01'}, format='json').data
        self.assertEqual((promo['code'], promo['is_valid']), ('RAMADAN20', True))
        self.assertEqual(self.client.post('/api/admin/shop/promos/', {'code': 'X', 'discount_percent': 95},
                                          format='json').status_code, 400)
        self.assertEqual(self.client.get('/api/admin/shop/stats/').data['low_stock'], 1)
        self.assertEqual(self.client.delete(f'/api/admin/shop/products/{pid}/').status_code, 204)


class NutritionAdminTests(ConsoleBase):
    def test_nutritionist_plan_review_and_scan(self):
        n = self.client.post('/api/admin/nutrition/nutritionists/', {'email': 'diet@t.local', 'first_name': 'Huda',
                                                                     'title': 'Dietitian'}, format='json')
        self.assertEqual(n.status_code, 201)
        self.client.force_authenticate(self.member)
        plan = self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': n.data['id'], 'goal': 'fat_loss'},
                                format='json').data
        self.assertIsNone(plan['reviewed_at'])
        self.client.force_authenticate(self.admin)
        self.assertIn('nutrition', [i['key'] for i in self.client.get('/api/admin/alerts/').data['items']])
        self.assertEqual(len(self.client.get('/api/admin/nutrition/plans/', {'status': 'to_review'}).data), 1)
        r = self.client.patch(f"/api/admin/nutrition/plans/{plan['id']}/", {'target_calories': 2100, 'notes': 'Walk daily'},
                              format='json')
        self.assertEqual((r.status_code, r.data['target_calories']), (200, 2100))
        self.assertTrue(Notification.objects.filter(recipient=self.member, title='Your nutrition plan was reviewed').exists())
        s = self.client.post(f'/api/admin/nutrition/members/{self.member.id}/scans/',
                             {'weight_kg': 70, 'body_fat_percent': 18.5, 'lean_mass_kg': 55, 'fat_mass_kg': 13}, format='json')
        self.assertEqual(s.status_code, 201)
        self.client.patch(f"/api/admin/nutrition/nutritionists/{n.data['id']}/", {'is_active': False}, format='json')
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get('/api/nutrition/nutritionists/').data, [])
        self.assertEqual(self.client.post('/api/nutrition/plan/request/', {'nutritionist_id': n.data['id'], 'goal': 'fat_loss'},
                                          format='json').status_code, 404)


class ActivityBadgeTests(ConsoleBase):
    def test_counts_new_activity_until_seen(self):
        from apps.catalog.models import Course, Enrollment, TrainerProfile
        t = TrainerProfile.objects.create(user=self.manager, title='Coach')
        c = Course.objects.create(trainer=t, title='Base', description='x', category='Strength', status='published')
        old = Enrollment.objects.create(member=self.member, course=c)
        # a new admin's first look: past activity isn't news to them
        self.assertEqual(set(self.client.get('/api/admin/activity/').data.values()), {0})
        self.client.force_authenticate(self.manager)
        self.client.get('/api/admin/activity/')
        old.delete()
        Enrollment.objects.create(member=self.member, course=c)              # happens after both first looks
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get('/api/admin/activity/').data['trainers'], 1)
        self.client.post('/api/admin/activity/', {'section': 'trainers'}, format='json')
        self.assertEqual(self.client.get('/api/admin/activity/').data['trainers'], 0)
        self.client.force_authenticate(self.manager)                      # per admin: the manager still sees it
        self.assertEqual(self.client.get('/api/admin/activity/').data['trainers'], 1)
        self.assertEqual(self.client.post('/api/admin/activity/', {'section': 'nope'}, format='json').status_code, 400)
