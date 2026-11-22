"""
Demo activity so every chart in the admin console and member portal has something to show:
members on each plan, 90 days of check-ins (realistic busy hours), payments and invoices,
workouts with logged sets, class bookings, 1-on-1 sessions, shop orders, meal logs, body scans
and reviews.

    python manage.py seed_demo_activity                # 40 members, last 90 days
    python manage.py seed_demo_activity --members 80 --days 120
    python manage.py seed_demo_activity --reset        # remove the demo data first (only it)

Everything it creates belongs to demo accounts (…@smartgym.demo), so --reset removes exactly
that and nothing real. Runs again safely: existing demo members are reused. If the gym has no
plans, coaches, machines, classes or products yet, a small demo catalogue is added too.
"""
import random
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

DEMO_DOMAIN = 'smartgym.demo'
FIRST = ['Sara', 'Omar', 'Lina', 'Faisal', 'Noor', 'Khalid', 'Huda', 'Yousef', 'Reem', 'Majed', 'Dana', 'Saad',
         'Layla', 'Hamad', 'Maha', 'Turki', 'Joud', 'Nasser', 'Rana', 'Fahad', 'Amal', 'Bandar', 'Hessa', 'Ziyad']
LAST = ['Alharbi', 'Alqahtani', 'Alotaibi', 'Alshehri', 'Alghamdi', 'Alzahrani', 'Aldosari', 'Almutairi',
        'Alanazi', 'Alshammari', 'Alsubaie', 'Almalki']
# Check-in hour weights: early-morning bump, lunch, big evening peak.
HOURS = {5: 2, 6: 6, 7: 7, 8: 4, 9: 2, 10: 2, 11: 2, 12: 4, 13: 3, 14: 2, 15: 3, 16: 6, 17: 10, 18: 12, 19: 11,
         20: 8, 21: 5, 22: 2}
REVIEWS = [
    (5, 'Fixed my squat form in two sessions. Clear cues and a plan I can follow.'),
    (5, 'Best coach I have trained with — always on time and keeps every session focused.'),
    (4, 'Great programming and very motivating. Booking slots fill fast though.'),
    (5, 'Lost 6 kg in three months without feeling hungry. The weekly check-ins help a lot.'),
    (4, 'Knows the science and explains it simply. Would recommend.'),
    (5, 'Finally consistent with training. The progress tracking keeps me honest.'),
]


class Command(BaseCommand):
    help = 'Create demo members and 90 days of gym activity so the charts have data (removable with --reset).'

    def add_arguments(self, parser):
        parser.add_argument('--members', type=int, default=40)
        parser.add_argument('--days', type=int, default=90)
        parser.add_argument('--reset', action='store_true', help='Delete existing demo data first.')
        parser.add_argument('--seed', type=int, default=7, help='Random seed (same seed → same data).')

    def handle(self, *args, **opts):
        self.rng = random.Random(opts['seed'])
        User = get_user_model()
        if opts['reset']:
            self.stdout.write(f'Removed demo data ({self.reset()} rows).')
        with transaction.atomic():
            catalogue = self.ensure_catalogue()
            members = self.ensure_members(opts['members'], opts['days'], catalogue)
            counts = self.activity(members, opts['days'], catalogue)
        from django.core.cache import cache
        cache.delete('public-live-v1')
        self.stdout.write(self.style.SUCCESS(
            f'Demo data ready: {len(members)} members · ' + ' · '.join(f'{v} {k}' for k, v in counts.items())))

    @transaction.atomic
    def reset(self):
        """Delete the demo accounts and everything that points at them (payments, sessions, plans…)."""
        from django.db.models import ProtectedError
        demo = get_user_model().objects.filter(email__endswith=f'@{DEMO_DOMAIN}')
        removed = 0
        for _ in range(10):          # protected rows (payments, sessions…) go first, then retry
            try:
                removed += demo.delete()[0]
                return removed
            except ProtectedError as exc:
                for obj in exc.protected_objects:
                    removed += obj.delete()[0] if obj.pk else 0
        raise RuntimeError('Demo data is still referenced by real records; nothing was removed.')

    # ------------------------------------------------------------ catalogue
    def ensure_catalogue(self):
        from apps.catalog.models import Course, TrainerProfile
        from apps.classes.models import GymClass
        from apps.members.models import MembershipPlan
        from apps.nutrition.models import NutritionistProfile
        from apps.shop.models import Product, ProductCategory
        from apps.workouts.models import Exercise
        User = get_user_model()

        plans = list(MembershipPlan.objects.filter(is_active=True))
        if not plans:
            plans = [MembershipPlan.objects.create(name=n, display_name=d, price_monthly=m, price_annual=a,
                                                   features=f)
                     for n, d, m, a, f in (('basic', 'Basic', 149, 1490, ['Gym floor', 'Locker']),
                                           ('pro', 'Pro', 249, 2490, ['Gym floor', 'Classes', 'Courses']),
                                           ('elite', 'Elite', 399, 3990, ['Everything in Pro', 'Recovery']))]

        coaches = list(TrainerProfile.objects.filter(user__is_active=True))
        if not coaches:
            for i, (first, last, title) in enumerate((('Omar', 'Saleh', 'Strength Coach'),
                                                      ('Reem', 'Fahad', 'Conditioning Coach'))):
                u = User.objects.create_user(username=f'coach{i}@{DEMO_DOMAIN}', email=f'coach{i}@{DEMO_DOMAIN}',
                                             password=None, role='trainer', first_name=first, last_name=last)
                coaches.append(TrainerProfile.objects.create(user=u, title=title, hourly_rate=Decimal('180'),
                                                             specialties=['Strength', 'Mobility'], is_verified=True))
        dietitians = list(NutritionistProfile.objects.filter(user__is_active=True))
        if not dietitians:
            u = User.objects.create_user(username=f'diet@{DEMO_DOMAIN}', email=f'diet@{DEMO_DOMAIN}', password=None,
                                         role='nutritionist', first_name='Huda', last_name='Nasser')
            dietitians = [NutritionistProfile.objects.create(user=u, title='Sports Dietitian', hourly_rate=Decimal('150'))]

        machines = list(Exercise.objects.filter(is_active=True)[:12])
        if not machines:
            for name, cat, muscle in (('Leg Press', 'strength', 'legs'), ('Chest Press', 'strength', 'chest'),
                                      ('Lat Pulldown', 'strength', 'back'), ('Shoulder Press', 'strength', 'shoulders'),
                                      ('Seated Row', 'strength', 'back'), ('Leg Curl', 'strength', 'legs')):
                machines.append(Exercise.objects.create(name=name, category=cat, primary_muscle=muscle,
                                                        qr_tag_id=f'SG-MCH-{uuid.uuid4().hex[:6].upper()}'))

        classes = list(GymClass.objects.filter(is_active=True))
        if not classes:
            for i, (name, cat, day, hour) in enumerate((('Morning HIIT', 'hiit', 0, 7), ('Strength Club', 'strength', 1, 18),
                                                        ('Mobility Flow', 'mobility', 2, 19), ('Spin', 'endurance', 3, 18),
                                                        ('Boxing Basics', 'combat', 5, 17))):
                classes.append(GymClass.objects.create(name=name, category=cat, day_of_week=day, start_time=time(hour),
                                                       capacity=12, location=f'Studio {i % 2 + 1}',
                                                       trainer=coaches[i % len(coaches)]))

        products = list(Product.objects.filter(is_active=True)[:8])
        if not products:
            cat, _ = ProductCategory.objects.get_or_create(key='supplements', defaults={'name': 'Supplements'})
            for i, (name, price) in enumerate((('Whey Protein 1kg', 189), ('Creatine 300g', 99), ('Shaker Bottle', 35))):
                products.append(Product.objects.create(name=name, sku=f'SUP-{i + 1:04d}', category=cat,
                                                       price=Decimal(price), stock_quantity=50))
        courses = list(Course.objects.filter(status='published'))
        return {'plans': plans, 'coaches': coaches, 'dietitians': dietitians, 'machines': machines,
                'classes': classes, 'products': products, 'courses': courses}

    # ------------------------------------------------------------ members
    def ensure_members(self, count, days, cat):
        from apps.members.models import Membership
        User = get_user_model()
        rng, today = self.rng, timezone.localdate()
        members = []
        for i in range(count):
            email = f'member{i + 1:03d}@{DEMO_DOMAIN}'
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(username=email, email=email, password=None, role='member',
                                                first_name=rng.choice(FIRST), last_name=rng.choice(LAST),
                                                phone=f'+9665{rng.randint(10000000, 99999999)}')
                joined = timezone.now() - timedelta(days=rng.randint(0, days + 60), hours=rng.randint(0, 23))
                User.objects.filter(id=user.id).update(created_at=joined, date_joined=joined)
                plan = rng.choices(cat['plans'], weights=[5, 3, 2][:len(cat['plans'])] or None)[0]
                months = rng.choice([1, 1, 3, 3, 6, 12])
                start = joined.date()
                expiry = start + timedelta(days=30 * months)
                while expiry < today - timedelta(days=rng.randint(0, 40)) and rng.random() < 0.8:
                    expiry += timedelta(days=30 * months)          # most members renewed
                status = 'active' if expiry > today + timedelta(days=7) else ('expiring' if expiry >= today else 'expired')
                Membership.objects.create(member=user, plan=plan, start_date=start, expiry_date=expiry, status=status,
                                          iqama_number=f'2{rng.randint(100000000, 999999999)}')
            members.append(user)
        return members

    # ------------------------------------------------------------ activity
    def activity(self, members, days, cat):
        from apps.catalog.models import Enrollment, TrainerReview, TrainerSession
        from apps.classes.models import ClassEnrollment
        from apps.members.models import CheckIn, Membership
        from apps.nutrition.models import DexaScan, MealLog, NutritionPlan, WaterIntake
        from apps.payments.models import Invoice, Transaction
        from apps.shop.models import Order, OrderItem
        from apps.workouts.models import Workout, WorkoutExercise, WorkoutSet

        rng, now, today = self.rng, timezone.now(), timezone.localdate()
        tz = timezone.get_current_timezone()
        hours, weights = list(HOURS), list(HOURS.values())
        counts = dict.fromkeys(('check-ins', 'workouts', 'sets', 'payments', 'sessions', 'orders', 'meals'), 0)

        def at(day, hour=None):
            h = hour if hour is not None else rng.choices(hours, weights)[0]
            return timezone.make_aware(datetime.combine(day, time(h, rng.randint(0, 59))), tz)

        def pay(member, when, kind, amount, text):
            ref = uuid.uuid4().hex[:8].upper()
            tx = Transaction.objects.create(member=member, transaction_id=f'TXN-D{ref}', description=text,
                                            transaction_type=kind, amount=amount, subtotal=amount, status='settled',
                                            payment_method_label='mada •••• 4242', gateway_reference=f'demo_{ref}')
            Transaction.objects.filter(id=tx.id).update(created_at=when, settled_at=when)
            inv = Invoice.objects.create(member=member, invoice_number=f'INV-D{ref}', transaction=tx, description=text,
                                         amount=amount, total=amount, status='paid', paid_at=when)
            Invoice.objects.filter(id=inv.id).update(issued_at=when)
            counts['payments'] += 1

        for m in members:
            ms = m.membership
            # membership payments: one per period since joining
            first = max(ms.start_date, today - timedelta(days=days))
            step = 30
            d = ms.start_date
            while d <= today:
                if d >= first:
                    pay(m, at(d, 10), 'subscription', ms.plan.price_monthly, f'Membership — {ms.plan.display_name}')
                d += timedelta(days=step)
            if CheckIn.objects.filter(member=m).exists():
                continue                                  # activity already generated for this member
            habit = rng.choice([1, 2, 3, 3, 4, 5])        # visits per week
            for offset in range(days, -1, -1):
                day = today - timedelta(days=offset)
                if day < ms.start_date or day > ms.expiry_date or rng.random() > habit / 7:
                    continue
                start = at(day)
                if start > now:
                    continue
                stay = rng.randint(45, 110)
                out = start + timedelta(minutes=stay)
                ci = CheckIn.objects.create(member=m, zone=rng.choice(['Iron Arena', 'Cardio Deck', 'Studio 1']),
                                            face_verified=True, duration_minutes=stay if out < now else None,
                                            check_out_time=out if out < now else None)
                CheckIn.objects.filter(id=ci.id).update(check_in_time=start)
                counts['check-ins'] += 1
                if cat['machines'] and rng.random() < 0.7:
                    w = Workout.objects.create(member=m, name='Training session', status='completed',
                                               completed_at=out, duration_minutes=stay)
                    Workout.objects.filter(id=w.id).update(started_at=start)
                    sets = reps = 0
                    tonnage = Decimal('0')
                    for order, ex in enumerate(rng.sample(cat['machines'], min(3, len(cat['machines'])))):
                        we = WorkoutExercise.objects.create(workout=w, exercise=ex, order=order)
                        base = Decimal(rng.choice([20, 30, 40, 50, 60, 80]))
                        for n in range(1, rng.randint(3, 5)):
                            r = rng.randint(6, 12)
                            s = WorkoutSet.objects.create(workout_exercise=we, set_number=n, reps=r, weight_kg=base,
                                                          rpe=Decimal(rng.choice([7, 8, 9])))
                            WorkoutSet.objects.filter(id=s.id).update(created_at=start + timedelta(minutes=5 * n))
                            sets, reps, tonnage = sets + 1, reps + r, tonnage + base * r
                    Workout.objects.filter(id=w.id).update(total_sets=sets, total_reps=reps, total_tonnage_kg=tonnage,
                                                           total_exercises=3)
                    counts['workouts'] += 1
                    counts['sets'] += sets
                if rng.random() < 0.25:
                    for meal in ('breakfast', 'lunch', 'dinner'):
                        ml = MealLog.objects.create(member=m, meal_type=meal, description=f'Demo {meal}',
                                                    calories=rng.randint(350, 850), protein_g=rng.randint(20, 55),
                                                    carbs_g=rng.randint(30, 90), fats_g=rng.randint(8, 30))
                        MealLog.objects.filter(id=ml.id).update(logged_at=at(day, 13), logged_date=day)
                        counts['meals'] += 1
                    WaterIntake.objects.filter(member=m, date=day).delete()
                    wi = WaterIntake.objects.create(member=m, amount_ml=rng.choice([1500, 2000, 2500, 3000]))
                    WaterIntake.objects.filter(id=wi.id).update(date=day)

            # classes, 1-on-1 coaching, shop, nutrition, scans, reviews
            for c in rng.sample(cat['classes'], min(len(cat['classes']), rng.randint(0, 2))):
                ClassEnrollment.objects.get_or_create(gym_class=c, member=m, defaults={'status': 'enrolled'})
            if cat['coaches'] and rng.random() < 0.35:
                coach = rng.choice(cat['coaches'])
                for _ in range(rng.randint(1, 4)):
                    when = at(today - timedelta(days=rng.randint(-10, days)), rng.choice([7, 9, 17, 18, 19]))
                    past = when < now
                    TrainerSession.objects.create(member=m, trainer=coach, scheduled_at=when,
                                                  status='completed' if past else 'scheduled',
                                                  amount_charged=coach.hourly_rate)
                    if past:
                        pay(m, when - timedelta(days=1), 'coaching', coach.hourly_rate, f'Session with {coach.user.full_name}')
                    counts['sessions'] += 1
                if rng.random() < 0.6:
                    rating, text = rng.choice(REVIEWS)
                    TrainerReview.objects.get_or_create(trainer=coach, member=m, defaults={'rating': rating, 'comment': text, 'status': 'approved'})
            if cat['products'] and rng.random() < 0.45:
                for _ in range(rng.randint(1, 3)):
                    when = at(today - timedelta(days=rng.randint(0, days)), 12)
                    p = rng.choice(cat['products'])
                    qty = rng.randint(1, 2)
                    o = Order.objects.create(member=m, order_number=f'ORD-D{uuid.uuid4().hex[:8].upper()}',
                                             status='completed', subtotal=p.price * qty, total=p.price * qty,
                                             completed_at=when)
                    Order.objects.filter(id=o.id).update(created_at=when)
                    OrderItem.objects.create(order=o, product=p, product_name=p.name, product_sku=p.sku,
                                             unit_price=p.price, quantity=qty, line_total=p.price * qty)
                    pay(m, when, 'retail', p.price * qty, f'Shop order {o.order_number}')
                    counts['orders'] += 1
            if cat['courses'] and rng.random() < 0.3:
                Enrollment.objects.get_or_create(member=m, course=rng.choice(cat['courses']))
            if cat['dietitians'] and rng.random() < 0.3:
                NutritionPlan.objects.get_or_create(member=m, status='active', defaults={
                    'nutritionist': rng.choice(cat['dietitians']), 'goal': rng.choice(['fat_loss', 'hypertrophy', 'recomp']),
                    'reviewed_at': now - timedelta(days=rng.randint(1, 30)), 'notes': 'Keep protein high and sleep 8 hours.'})
                weight = Decimal(rng.randint(62, 98))
                fat = Decimal(rng.randint(16, 30))
                for k in range(3):
                    w = weight - k * Decimal('1.2')
                    f = fat - k
                    scan = DexaScan.objects.create(member=m, weight_kg=w, body_fat_percent=f,
                                                   fat_mass_kg=(w * f / 100).quantize(Decimal('0.01')),
                                                   lean_mass_kg=(w * (100 - f) / 100).quantize(Decimal('0.01')))
                    DexaScan.objects.filter(id=scan.id).update(scan_date=today - timedelta(days=30 * (2 - k)))
            visits = CheckIn.objects.filter(member=m).count()
            Membership.objects.filter(id=ms.id).update(total_check_ins=visits)

        for coach in cat['coaches']:
            from django.db.models import Avg, Count
            agg = coach.reviews.aggregate(a=Avg('rating'), n=Count('id'))
            type(coach).objects.filter(id=coach.id).update(rating=round(agg['a'] or 0, 2), total_reviews=agg['n'])
        return counts
