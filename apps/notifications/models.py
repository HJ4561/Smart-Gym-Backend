from django.db import models
from django.conf import settings


class Notification(models.Model):
    """A notification sent to a user."""
    CATEGORY_CHOICES = [
        ('workout', 'Workout'),
        ('course', 'Course'),
        ('recovery', 'Recovery'),
        ('payment', 'Payment'),
        ('achievement', 'Achievement'),
        ('nutrition', 'Nutrition'),
        ('trainer', 'Trainer'),
        ('shop', 'Shop'),
        ('attendance', 'Attendance'),
        ('system', 'System'),
    ]
    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('normal', 'Normal'),
        ('high', 'High'),
    ]

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='system')
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='normal')

    icon = models.CharField(max_length=40, default='notifications')
    title = models.CharField(max_length=200)
    message = models.TextField()
    title_ar = models.CharField(max_length=200, blank=True, db_default='')   # the same notification in Arabic
    message_ar = models.TextField(blank=True, db_default='')

    # Action
    action_label = models.CharField(max_length=80, blank=True)
    action_path = models.CharField(max_length=200, blank=True)

    # State
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notifications'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.recipient.email} — {self.title}"


class NotificationPreference(models.Model):
    """Per-user notification settings."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notification_preferences',
    )
    push_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)
    whatsapp_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=False)
    marketing_enabled = models.BooleanField(default=True)

    class Meta:
        db_table = 'notification_preferences'

    def __str__(self):
        return f"Prefs — {self.user.email}"