from django.db import models
from django.conf import settings
from apps.core.sides import SideManager, side_field


class StaffProfile(models.Model):
    """
    Extra profile for staff members (non-members).
    Linked to a User with role in: admin, manager, trainer, receptionist, nutritionist.
    """
    DEPARTMENT_CHOICES = [
        ('executive', 'Executive'),
        ('operations', 'Operations'),
        ('coaching', 'Coaching'),
        ('clinical', 'Clinical'),
        ('front_desk', 'Front Desk'),
        ('retail', 'Shop'),
        ('facility', 'Facility'),
    ]

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('off_shift', 'Off Shift'),
        ('on_leave', 'On Leave'),
        ('suspended', 'Suspended'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='staff_profile',
    )
    department = models.CharField(max_length=30, choices=DEPARTMENT_CHOICES, default='operations')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')

    shift_start = models.TimeField(null=True, blank=True)
    shift_end = models.TimeField(null=True, blank=True)

    permissions_count = models.IntegerField(default=0)
    # Front-desk modules an admin gave this receptionist (apps.core.roles.DESK_MODULES keys).
    modules = models.JSONField(default=list, blank=True)
    joined_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'staff_profiles'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.full_name} ({self.department})"

    @property
    def shift_display(self):
        if self.shift_start and self.shift_end:
            return f"{self.shift_start.strftime('%H:%M')} - {self.shift_end.strftime('%H:%M')}"
        return 'Full Access'