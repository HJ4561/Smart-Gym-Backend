from django.db import models
from django.conf import settings


class Milestone(models.Model):
    """Milestone reward tiers (streak-based)."""
    TIER_TYPES = [
        ('consistency', 'Consistency'),
        ('streak', 'Streak'),
        ('challenge', 'Challenge'),
    ]

    tier = models.CharField(max_length=60)         # "Tier 01 • Unlocked"
    tier_ar = models.CharField(max_length=60, blank=True, db_default='')   # Arabic; empty = show the English
    title = models.CharField(max_length=120)       # "15-Day Consistency"
    title_ar = models.CharField(max_length=120, blank=True, db_default='')   # Arabic; empty = show the English
    description = models.TextField(blank=True)
    description_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English
    icon = models.CharField(max_length=40, default='emoji_events')

    tier_type = models.CharField(max_length=20, choices=TIER_TYPES, default='consistency')
    target_days = models.IntegerField(default=15)
    reward_value = models.CharField(max_length=200, blank=True)  # "$65 value cryo session"
    reward_value_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    reward_code = models.CharField(max_length=80, blank=True)    # "CONSISTENT-15"

    display_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'milestones'
        ordering = ['display_order']

    def __str__(self):
        return f"{self.tier} — {self.title}"


class MemberMilestone(models.Model):
    """Per-member milestone progress & claims."""
    STATUS_CHOICES = [
        ('locked', 'Locked'),
        ('active', 'In Progress'),
        ('claimed', 'Claimed'),
    ]

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='milestones',
    )
    milestone = models.ForeignKey(Milestone, on_delete=models.CASCADE)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='locked')
    progress = models.IntegerField(default=0)      # 0-100
    current_days = models.IntegerField(default=0)
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'member_milestones'
        unique_together = ['member', 'milestone']
        ordering = ['milestone__display_order']

    def __str__(self):
        return f"{self.member.email} — {self.milestone.title}"