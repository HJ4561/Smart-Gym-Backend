from django.db import models
from django.conf import settings


# ============================================
# TRAINERS
# ============================================
class TrainerProfile(models.Model):
    """Extended profile for users with role=trainer."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='trainer_profile',
    )
    title = models.CharField(max_length=120)  # "Head Elite Specialist"
    title_ar = models.CharField(max_length=120, blank=True, db_default='')   # Arabic; empty = show the English
    credentials = models.CharField(max_length=200, blank=True)  # "CSCS*D, USAW"
    credentials_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    bio = models.TextField(blank=True)
    bio_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English
    photo_url = models.URLField(blank=True)

    # Verification
    is_verified = models.BooleanField(default=False)
    is_accepting_clients = models.BooleanField(default=True)

    # Specialties (list of strings)
    specialties = models.JSONField(default=list, blank=True)
    specialties_ar = models.JSONField(default=list, blank=True, null=True)   # Arabic; empty = show the English

    # Metrics
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    total_reviews = models.IntegerField(default=0)
    total_students = models.IntegerField(default=0)
    years_experience = models.IntegerField(default=0)

    # Pricing
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, default=95)

    # Session types
    offers_1on1 = models.BooleanField(default=True)
    offers_consultation = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'trainer_profiles'
        ordering = ['-rating']

    def __str__(self):
        return f"{self.user.full_name} — {self.title}"


class TrainerAvailability(models.Model):
    """Time slots when a trainer is available for 1:1 booking."""
    DAY_CHOICES = [
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'),
        (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    ]

    trainer = models.ForeignKey(
        TrainerProfile,
        on_delete=models.CASCADE,
        related_name='availability',
    )
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'trainer_availability'
        ordering = ['day_of_week', 'start_time']

    def __str__(self):
        return f"{self.trainer} — {self.get_day_of_week_display()} {self.start_time}-{self.end_time}"


# ============================================
# COURSES
# ============================================
class Course(models.Model):
    """A training course/program published by a trainer."""
    LEVEL_CHOICES = [
        ('beginner', 'Beginner'),
        ('intermediate', 'Intermediate'),
        ('advanced', 'Advanced'),
        ('all_levels', 'All Levels'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('pending', 'Pending Review'),
        ('published', 'Published'),
        ('archived', 'Archived'),
    ]

    trainer = models.ForeignKey(
        TrainerProfile,
        on_delete=models.PROTECT,
        related_name='courses',
    )
    title = models.CharField(max_length=200)
    title_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    subtitle = models.CharField(max_length=200, blank=True)
    subtitle_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    description = models.TextField()
    description_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English
    category = models.CharField(max_length=60)  # Hypertrophy, VBT, Combat, etc.
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='intermediate')

    # Media
    cover_image_url = models.URLField(blank=True)
    promo_video_url = models.URLField(blank=True)

    # Structure
    duration_weeks = models.IntegerField(default=8)
    days_per_week = models.IntegerField(default=4)
    estimated_minutes_per_session = models.IntegerField(default=60)

    # Pricing
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    is_included_with_pro = models.BooleanField(default=False)

    # Status — coaches write drafts and send them for review (pending); the gym publishes them.
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    is_featured = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)       # last sent for review
    review_note = models.TextField(blank=True, db_default='')       # why the gym sent it back

    # Metrics
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    total_reviews = models.IntegerField(default=0)
    total_enrolled = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'courses'
        ordering = ['-is_featured', '-created_at']

    def __str__(self):
        return self.title


class CourseModule(models.Model):
    """A day/week block within a course."""
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name='modules',
    )
    order = models.IntegerField()
    title = models.CharField(max_length=200)
    title_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    description = models.TextField(blank=True)
    description_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English
    week_number = models.IntegerField(default=1)
    day_number = models.IntegerField(default=1)

    # Content
    video_url = models.URLField(blank=True)
    pdf_url = models.URLField(blank=True)
    duration_minutes = models.IntegerField(default=30)
    is_locked_until_prev = models.BooleanField(default=True)

    class Meta:
        db_table = 'course_modules'
        ordering = ['order']

    def __str__(self):
        return f"{self.course.title} — {self.title}"


class Enrollment(models.Model):
    """A member enrolled in a course."""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('paused', 'Paused'),
        ('refunded', 'Refunded'),
    ]

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='enrollments',
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name='enrollments',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    enrolled_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Progress
    completed_modules = models.ManyToManyField(
        CourseModule,
        blank=True,
        related_name='completed_by',
    )
    progress_percent = models.IntegerField(default=0)

    # Payment
    amount_paid = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    payment_ref = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = 'enrollments'
        unique_together = ['member', 'course']
        ordering = ['-enrolled_at']

    def __str__(self):
        return f"{self.member.email} — {self.course.title}"


# ============================================
# 1:1 SESSIONS
# ============================================
class TrainerSession(models.Model):
    """A booked 1:1 session with a trainer."""
    STATUS_CHOICES = [
        ('pending_payment', 'Awaiting payment'),
        ('scheduled', 'Scheduled'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('no_show', 'No Show'),
    ]

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='trainer_sessions',
    )
    trainer = models.ForeignKey(
        TrainerProfile,
        on_delete=models.PROTECT,
        related_name='sessions',
    )

    scheduled_at = models.DateTimeField()
    duration_minutes = models.IntegerField(default=60)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')

    # Session details
    focus_area = models.CharField(max_length=120, blank=True)  # "Technique Analysis"
    notes = models.TextField(blank=True)

    # Pricing
    amount_charged = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'trainer_sessions'
        ordering = ['-scheduled_at']

    def __str__(self):
        return f"{self.member.email} ↔ {self.trainer} @ {self.scheduled_at:%Y-%m-%d %H:%M}"


# ============================================
# REVIEWS
# ============================================
class CourseReview(models.Model):
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name='reviews',
    )
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    rating = models.IntegerField()  # 1-5
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'course_reviews'
        unique_together = ['course', 'member']
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.course.title} — {self.rating}★"


class TrainerReview(models.Model):
    trainer = models.ForeignKey(
        TrainerProfile,
        on_delete=models.CASCADE,
        related_name='reviews',
    )
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    rating = models.IntegerField()  # 1-5
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'trainer_reviews'
        unique_together = ['trainer', 'member']
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.trainer} — {self.rating}★"

def enrollment_started(enrollment):
    """Tell the coach their course has a new student and the gym about the purchase."""
    from apps.core.i18n import ar, bi
    from apps.notifications.service import notify_coach, notify_staff
    course, member = enrollment.course, enrollment.member
    who = member.full_name or member.email
    paid = enrollment.amount_paid or 0
    title_ar = ar(course, 'title')
    notify_coach(course.trainer, 'course', bi('A member started your course', 'بدأ عضو دورتك'),
                 bi(f'{who} enrolled in “{course.title}” and is starting Day 1.',
                    f'اشترك {who} في «{title_ar}» ويبدأ اليوم الأول.'), icon='school',
                 action_label='My students', action_path='/coach/students')
    notify_staff('course', bi('Course purchased', 'تم شراء دورة') if paid > 0 else bi('Course enrolment', 'اشتراك في دورة'),
                 bi(f'{who} enrolled in “{course.title}”' + (f' — paid SAR {paid}.' if paid > 0 else ' (free).'),
                    f'اشترك {who} في «{title_ar}»' + (f' — دفع {paid} ريال.' if paid > 0 else ' (مجانًا).')),
                 icon='school', action_label='Open courses', action_path='/admin/trainers?tab=courses')
