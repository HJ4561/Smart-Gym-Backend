from django.db import models
from django.conf import settings
from apps.core.sides import SideManager, side_field


class SystemSettings(models.Model):
    """
    One row per gym side — use get_solo().
    Stores all admin-configurable settings as JSON.
    """
    general = models.JSONField(default=dict, blank=True)
    roles = models.JSONField(default=dict, blank=True)
    integrations = models.JSONField(default=dict, blank=True)
    security = models.JSONField(default=dict, blank=True)
    notifications = models.JSONField(default=dict, blank=True)
    billing = models.JSONField(default=dict, blank=True)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'system_settings'
        verbose_name_plural = 'System Settings'
        constraints = [models.UniqueConstraint(fields=['side'], name='uniq_system_settings_per_side')]

    def __str__(self):
        return 'System Settings'

    @classmethod
    def get_solo(cls):
        """This gym side's settings (one row per side; the original row, pk=1, is the male side's)."""
        from apps.core.sides import side_default
        obj = cls._base_manager.filter(side=side_default()).first()
        if obj is None:
            obj, _ = cls._base_manager.get_or_create(side=side_default())
        return obj

class AdminSeen(models.Model):
    """When an admin last opened a sidebar section — new activity after this shows as a badge."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='admin_seen')
    section = models.CharField(max_length=40)
    seen_at = models.DateTimeField()

    class Meta:
        db_table = 'admin_seen'
        unique_together = ('user', 'section')
