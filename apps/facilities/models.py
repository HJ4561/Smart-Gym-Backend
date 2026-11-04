from django.db import models
from django.conf import settings


class Facility(models.Model):
    CATEGORY_CHOICES = [
        ('gym', 'Gym Floor'),
        ('recovery', 'Recovery'),
        ('aquatic', 'Aquatic'),
        ('sports', 'Sports Court'),
        ('combat', 'Combat'),
    ]

    name = models.CharField(max_length=120)
    name_ar = models.CharField(max_length=120, blank=True, db_default='')   # Arabic; empty = show the English
    code = models.CharField(max_length=20, unique=True)  # FAC-01
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='gym')
    zone = models.CharField(max_length=60, blank=True)  # Sector 01
    description = models.TextField(blank=True)
    description_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English

    capacity = models.IntegerField(default=10)
    current_occupancy = models.IntegerField(default=0)

    temp_celsius = models.DecimalField(max_digits=5, decimal_places=2, default=22)
    amenities = models.JSONField(default=list, blank=True)

    is_active = models.BooleanField(default=True)
    is_bookable = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'facilities'
        ordering = ['code']

    def __str__(self):
        return f"{self.code} — {self.name}"

    @property
    def utilization(self):
        if self.capacity <= 0:
            return 0
        return round((self.current_occupancy / self.capacity) * 100)

    @property
    def status(self):
        pct = self.utilization
        if pct >= 90:
            return 'full'
        if pct >= 70:
            return 'high'
        if pct >= 40:
            return 'moderate'
        return 'low'


class FacilityBooking(models.Model):
    """A booking for a facility."""
    STATUS_CHOICES = [
        ('booked', 'Booked'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('no_show', 'No Show'),
    ]

    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='bookings')
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='facility_bookings',
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='booked')
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'facility_bookings'
        ordering = ['-start_time']

    def __str__(self):
        return f"{self.facility.name} — {self.member.email} @ {self.start_time}"