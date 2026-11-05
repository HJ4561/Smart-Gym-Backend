from django.db import models
from django.conf import settings
from django.utils import timezone
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from dateutil.relativedelta import relativedelta
import uuid


# ============================================================
# MEMBERSHIP PLANS
# ============================================================
class MembershipPlan(models.Model):
    """Available membership tiers. Edited by admin, displayed on public site."""
    # Short unique code ("basic", "pro", "black" were the originals; admins can add more).
    name = models.CharField(max_length=50, unique=True)
    display_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price_monthly = models.DecimalField(max_digits=8, decimal_places=2)
    price_annual = models.DecimalField(max_digits=8, decimal_places=2)
    features = models.JSONField(default=list, blank=True)
    # Arabic copy for the public site (db defaults so pre-deploy code can still insert plans).
    display_name_ar = models.CharField(max_length=100, blank=True, db_default='')
    description_ar = models.TextField(blank=True, db_default='')
    features_ar = models.JSONField(default=list, blank=True, null=True)
    # Max length of one gym visit; empty = unlimited.
    session_limit_minutes = models.PositiveIntegerField(
        null=True, blank=True, help_text='Max minutes per visit. Leave empty for unlimited.',
    )
    # Machine Guide videos included with the plan: basic (free ones only), advanced, or premium (all).
    VIDEO_ACCESS_CHOICES = [('basic', 'Basic videos'), ('advanced', 'Basic + Advanced'), ('premium', 'All videos')]
    video_access = models.CharField(max_length=10, choices=VIDEO_ACCESS_CHOICES, default='basic', db_default='basic')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'membership_plans'
        ordering = ['price_monthly']

    def __str__(self):
        return self.display_name


# ============================================================
# MEMBERSHIP
# ============================================================
class Membership(models.Model):
    """A member's active membership."""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expiring', 'Expiring Soon'),
        ('expired', 'Expired'),
        ('frozen', 'Frozen'),
    ]

    member = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='membership',
    )
    plan = models.ForeignKey(
        MembershipPlan,
        on_delete=models.PROTECT,
        related_name='memberships',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    start_date = models.DateField()
    expiry_date = models.DateField()
    auto_renew = models.BooleanField(default=True)
    is_frozen = models.BooleanField(default=False)
    frozen_until = models.DateField(null=True, blank=True)

    # ⬇️ NEW — for gym entry lookup (Iqama = 10-digit Saudi residency ID)
    iqama_number = models.CharField(max_length=20, blank=True, db_index=True)

    # Digital pass
    pass_id = models.CharField(max_length=20, unique=True, blank=True)
    nfc_token = models.CharField(max_length=64, blank=True)

    # Stats
    lifetime_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_check_ins = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'memberships'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        # Auto-generate pass_id if missing — MUST run before super().save()
        if not self.pass_id:
            self.pass_id = f"TK-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.member.email} — {self.plan.display_name}"

    @property
    def days_remaining(self):
        delta = self.expiry_date - date.today()
        return max(0, delta.days)

    @property
    def time_remaining(self):
        """
        Membership countdown, which starts on start_date (= the day the
        registration was approved). Calendar months + leftover days, so it
        matches how the plan was sold: a 3-month plan approved today shows
        {"months": 3, "days": 0}. Also the plan's total length and how much is used.
        """
        today = date.today()
        total_days = max(0, (self.expiry_date - self.start_date).days)
        if self.expiry_date <= today:
            months = days = left = 0
        else:
            rd = relativedelta(self.expiry_date, max(today, self.start_date))
            months, days = rd.years * 12 + rd.months, rd.days
            left = (self.expiry_date - today).days
        used = min(total_days, max(0, (today - self.start_date).days))
        return {
            'months': months,
            'days': days,
            'total_days_remaining': left,
            'plan_total_days': total_days,
            'percent_used': round(100 * used / total_days) if total_days else 100,
            'expired': self.expiry_date <= today,
        }

    ENTRY_STATUSES = ('active', 'expiring')   # "expiring soon" is still paid up

    @property
    def effectively_frozen(self):
        """Frozen right now. A freeze ends by itself on frozen_until (no job needed)."""
        return bool(self.is_frozen and (self.frozen_until is None or self.frozen_until > date.today()))

    @property
    def allows_entry(self):
        """Single rule for gym entry — used by the gate lookup and the check-in endpoint."""
        if self.effectively_frozen:
            return False
        status_ok = self.status in self.ENTRY_STATUSES or (self.status == 'frozen' and self.is_frozen)
        return status_ok and self.days_remaining > 0


# ============================================================
# CHECK-IN
# ============================================================
class CheckIn(models.Model):
    """A single gym check-in record."""
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='check_ins',
    )
    facility = models.CharField(max_length=100, default='Metro Station Core 01')
    zone = models.CharField(max_length=100, blank=True)
    check_in_time = models.DateTimeField(auto_now_add=True)
    check_out_time = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.IntegerField(null=True, blank=True)
    # Session timer — snapshot of the plan's limit at check-in time (empty = unlimited).
    session_limit_minutes = models.PositiveIntegerField(null=True, blank=True)
    expected_checkout_time = models.DateTimeField(null=True, blank=True)

    # ⬇️ NEW — staff verification trail
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='verified_check_ins',
    )
    face_verified = models.BooleanField(default=False)

    class Meta:
        db_table = 'check_ins'
        ordering = ['-check_in_time']

    def __str__(self):
        return f"{self.member.email} @ {self.check_in_time}"


# ============================================================
# FACE CHECK (security audit trail — every gate comparison)
# ============================================================
class FaceCheck(models.Model):
    """
    One face comparison at the entrance, granted or denied. Kept even if the
    member is later deleted (member is SET_NULL; iqama_number is a snapshot).
    """
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='face_checks',
    )
    iqama_number = models.CharField(max_length=20, blank=True, db_index=True)
    matched      = models.BooleanField()
    confidence   = models.FloatField()                       # similarity score
    threshold    = models.FloatField(null=True, blank=True)  # threshold in force
    verified_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='performed_face_checks',
    )
    check_in     = models.OneToOneField(
        CheckIn,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='face_check',
    )
    source       = models.CharField(max_length=30, default='gate')
    created_at   = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'face_checks'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['matched', '-created_at'])]

    def __str__(self):
        return f"{'MATCH' if self.matched else 'DENIED'} {self.iqama_number} ({self.confidence:.3f}) @ {self.created_at}"


# ============================================================
# MEMBERSHIP REGISTRATION (new — public signup pipeline)
# ============================================================
class MembershipRegistration(models.Model):
    """
    A pending public-site registration. Sits here until an admin approves it.
    On approval → creates a real User + Membership + Transaction.
    """
    STATUS_PENDING   = 'pending'
    STATUS_PAID      = 'payment_confirmed'
    STATUS_APPROVED  = 'approved'
    STATUS_REJECTED  = 'rejected'
    STATUS_EXPIRED   = 'expired'

    STATUS = [
        (STATUS_PENDING,  'Pending'),
        (STATUS_PAID,     'Pending admin approval'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_EXPIRED,  'Expired'),
    ]

    GENDER_CHOICES = [
        ('male', 'Male'),
        ('female', 'Female'),
        ('other', 'Other'),
    ]

    PAYMENT_STATUS = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]

    # Applicant info
    iqama_number = models.CharField(max_length=20, unique=True, db_index=True)
    full_name    = models.CharField(max_length=150)
    email        = models.EmailField(unique=True)
    phone        = models.CharField(max_length=20)
    address      = models.TextField(blank=True)
    # Picked on the signup map (Google Maps); optional.
    address_lat  = models.FloatField(null=True, blank=True)
    address_lng  = models.FloatField(null=True, blank=True)
    age          = models.IntegerField()
    gender       = models.CharField(max_length=20, choices=GENDER_CHOICES)
    photo        = models.ImageField(upload_to='registrations/%Y/%m/', blank=True, null=True)

    # Chosen plan
    plan            = models.ForeignKey(MembershipPlan, on_delete=models.PROTECT, related_name='registrations')
    duration_months = models.IntegerField(default=1)

    # Payment (Moyasar — Mada / STC Pay)
    payment_method    = models.CharField(max_length=20, default='mada')
    payment_reference = models.CharField(max_length=100, blank=True)
    payment_amount    = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Facility add-ons chosen at signup: [{id, name, monthly_price, months, amount}] (prices frozen here).
    facilities        = models.JSONField(default=list, blank=True, null=True)
    referral_code     = models.CharField(max_length=12, blank=True, db_default='')   # who sent them (checked at signup)
    payment_status    = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='pending')
    payment_paid_at   = models.DateTimeField(null=True, blank=True)
    # null (not '') so the unique constraint ignores registrations without a payment yet
    moyasar_payment_id     = models.CharField(max_length=64, unique=True, null=True, blank=True)
    # Server-issued per attempt and sent in the payment's metadata; verification requires
    # it to match, so a payment can't be bound to a registration via client metadata alone.
    payment_attempt_token  = models.CharField(max_length=64, blank=True, db_default='')
    payment_failure_reason = models.CharField(max_length=255, blank=True, db_default='')
    payment_raw            = models.JSONField(null=True, blank=True)  # last payment object fetched from Moyasar

    # Meta
    language         = models.CharField(max_length=2, choices=[('en', 'English'), ('ar', 'Arabic')],
                                        db_default='en', default='en')   # emails before there is an account
    status           = models.CharField(max_length=30, choices=STATUS, default=STATUS_PENDING)
    submitted_at     = models.DateTimeField(auto_now_add=True)
    approved_at      = models.DateTimeField(null=True, blank=True)
    approved_by      = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='approved_registrations',
    )
    rejection_reason = models.TextField(blank=True)

    # Password (hashed — real User created only on approval)
    password_hash = models.CharField(max_length=255)

    # Link to created user (after approval)
    created_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='source_registration',
    )

    class Meta:
        db_table = 'membership_registrations'
        ordering = ['-submitted_at']
        indexes = [
            models.Index(fields=['status', '-submitted_at']),
            models.Index(fields=['email']),
        ]

    def __str__(self):
        return f"#{self.id} {self.full_name} — {self.get_status_display()}"

    @property
    def is_actionable(self):
        return self.status in (self.STATUS_PENDING, self.STATUS_PAID)

    @property
    def amount_halalas(self):
        """Server-side expected charge in halalas (Moyasar's smallest unit)."""
        return int((Decimal(self.payment_amount) * 100).to_integral_value(ROUND_HALF_UP))

    @property
    def total_price(self):
        """Simple linear price: monthly * months (no annual discount logic here)."""
        if self.duration_months >= 12:
            # 12 months at annual rate
            years = self.duration_months / 12
            return round(float(self.plan.price_annual) * years, 2)
        return float(self.plan.price_monthly) * self.duration_months


# ============================================================
# FACILITY ADD-ONS (pool, courts, recovery... paid per month)
# ============================================================
class FacilityAddon(models.Model):
    """
    Price of a public-site facility (an item of the admin "Facilities" list).
    Members add it at signup (price x membership months) or later from the
    portal (price x months remaining). Set in Admin -> Facility Pricing.
    """
    item = models.OneToOneField('site_content.SiteItem', on_delete=models.CASCADE, related_name='addon')
    monthly_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    is_bookable = models.BooleanField(default=False)
    capacity = models.PositiveIntegerField(default=0)        # people at once; feeds live occupancy
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'facility_addons'

    def __str__(self):
        return f'{self.name} — SAR {self.monthly_price}/mo'

    @property
    def name(self):
        return ((self.item.data or {}).get('title') or {}).get('en') or f'Facility #{self.item_id}'


class MembershipFacility(models.Model):
    """A member's paid access to one facility, until `expires_on` (the membership expiry when bought)."""
    SOURCE_CHOICES = [('signup', 'Signup'), ('addon', 'Added later')]

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='facility_access')
    item = models.ForeignKey('site_content.SiteItem', on_delete=models.SET_NULL, null=True, related_name='+')
    facility_name = models.CharField(max_length=200)                 # snapshot
    monthly_price = models.DecimalField(max_digits=8, decimal_places=2)
    months = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    starts_on = models.DateField()
    expires_on = models.DateField()
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'membership_facilities'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.member} — {self.facility_name} until {self.expires_on}'

    @property
    def is_active(self):
        return self.expires_on > date.today()


class Referral(models.Model):
    """A signup made with a member's referral link, approved by the gym: the referrer got `reward` in credit."""
    referrer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='referrals_made')
    referred = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='referred_by')
    registration = models.OneToOneField(MembershipRegistration, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='referral')
    reward = models.DecimalField(max_digits=8, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'referrals'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.referrer} → {self.referred} (SAR {self.reward})'


class LivenessCheck(models.Model):
    """
    Proof that a live person was in front of the signup camera (apps.members.liveness): they
    looked straight, then turned their head to one side and to the other — a printed or on-screen
    photo can't. Registration accepts only the photo taken at the end of a passed check.
    """
    token_hash = models.CharField(max_length=64, unique=True)
    stage = models.CharField(max_length=10, default='straight')     # straight → turn1 → turn2 → done
    first_turn = models.SmallIntegerField(default=0)                # -1 / +1: which way the first turn went
    frames = models.PositiveSmallIntegerField(default=0)
    face_width = models.FloatField(null=True, blank=True)           # the face should stay about the same size
    photo_sha256 = models.CharField(max_length=64, blank=True)      # the photo taken after it passed
    used = models.BooleanField(default=False)                       # one registration per check
    ip = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'liveness_checks'
