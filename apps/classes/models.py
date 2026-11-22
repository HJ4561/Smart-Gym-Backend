from django.db import models
from django.conf import settings
from apps.catalog.models import TrainerProfile
from apps.core.sides import SideManager, side_field


class GymClass(models.Model):
    """A scheduled fitness class."""
    LEVEL_CHOICES = [
        ('beginner', 'Beginner'),
        ('intermediate', 'Intermediate'),
        ('advanced', 'Advanced'),
        ('all_levels', 'All Levels'),
    ]
    CATEGORY_CHOICES = [
        ('strength', 'Strength'),
        ('hiit', 'HIIT'),
        ('combat', 'Combat'),
        ('mobility', 'Mobility'),
        ('endurance', 'Endurance'),
        ('olympic', 'Olympic Lifting'),
        ('recovery', 'Recovery'),
    ]

    name = models.CharField(max_length=120)
    name_ar = models.CharField(max_length=120, blank=True, db_default='')   # Arabic; empty = show the English
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='all_levels')
    description = models.TextField(blank=True)
    description_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English

    trainer = models.ForeignKey(
        TrainerProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='classes',
    )

    day_of_week = models.IntegerField()  # 0 = Mon, 6 = Sun
    start_time = models.TimeField()
    duration_minutes = models.IntegerField(default=60)

    capacity = models.IntegerField(default=10)
    location = models.CharField(max_length=100, blank=True)
    location_ar = models.CharField(max_length=100, blank=True, db_default='')   # Arabic; empty = show the English

    # Paid classes (0 = free). A class can run for a set period (starts_on → ends_on): members buy
    # the whole run, or pay for single sessions.
    price_per_session = models.DecimalField(max_digits=8, decimal_places=2, default=0, db_default=0)
    full_price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'gym_classes'
        ordering = ['day_of_week', 'start_time']

    def __str__(self):
        return f"{self.name} @ {self.day_of_week} {self.start_time}"


class ClassEnrollment(models.Model):
    """A member enrollment in a gym class."""
    STATUS_CHOICES = [
        ('enrolled', 'Enrolled'),
        ('waitlist', 'Waitlist'),
        ('attended', 'Attended'),
        ('no_show', 'No Show'),
        ('cancelled', 'Cancelled'),
    ]

    gym_class = models.ForeignKey(GymClass, on_delete=models.CASCADE, related_name='enrollments')
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='class_enrollments',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='enrolled')
    enrolled_at = models.DateTimeField(auto_now_add=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'class_enrollments'
        unique_together = ['gym_class', 'member']

    def __str__(self):
        return f"{self.gym_class.name} — {self.member.email}"


class ClassPass(models.Model):
    """
    What a member paid for a paid class: the whole run (kind=full, valid to the class's last day)
    or one session (kind=session, on session_date). Free classes need no pass.
    """
    KIND_CHOICES = [('full', 'Whole class'), ('session', 'One session')]

    gym_class = models.ForeignKey(GymClass, on_delete=models.PROTECT, related_name='passes')
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='class_passes')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    session_date = models.DateField(null=True, blank=True)      # kind=session
    valid_until = models.DateField()                            # last day it covers
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    is_cancelled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'class_passes'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.gym_class.name} — {self.member.email} ({self.kind})'


class ClassPost(models.Model):
    """
    A coach's note or video for the members of one of their classes (optionally about one date).
    Messages reach the class at once; videos wait for an admin first (apps.approvals).
    """
    KIND_CHOICES = [('message', 'Message'), ('video', 'Video')]
    APPROVAL = [('pending', 'Waiting for approval'), ('approved', 'Approved'), ('rejected', 'Not approved')]

    gym_class = models.ForeignKey(GymClass, on_delete=models.CASCADE, related_name='posts')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='message')
    for_date = models.DateField(null=True, blank=True)          # one session of the class, or the class in general
    title = models.CharField(max_length=120)
    body = models.TextField(blank=True)
    video_url = models.CharField(max_length=300, blank=True)
    thumbnail_url = models.CharField(max_length=500, blank=True)
    approval_status = models.CharField(max_length=10, choices=APPROVAL, default='approved')
    created_at = models.DateTimeField(auto_now_add=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'class_posts'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.gym_class.name} — {self.title}'
