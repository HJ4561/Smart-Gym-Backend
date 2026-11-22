"""
The admin's approval queue and the staff activity log.

ChangeRequest — something a coach, nutritionist, shop manager or member sent that members don't see
until an admin approves it:
  create  a new item, saved hidden (approval_status / status 'pending') until approved
  update  proposed new values for a live item; the live version stays as it is until approved
  delete  a request to remove a live item
The admin can change the proposed values before approving (`payload`), or reject with a note.

StaffActivity — every change a staff member makes (one row per successful write request), so the
admin can see who did what and when.
"""
from django.conf import settings
from django.db import models
from apps.core.sides import SideManager, side_field


class ChangeRequest(models.Model):
    KIND_CHOICES = [('create', 'New'), ('update', 'Change'), ('delete', 'Remove')]
    STATUS_CHOICES = [('pending', 'Waiting'), ('approved', 'Approved'), ('rejected', 'Rejected'),
                      ('withdrawn', 'Withdrawn')]

    target_type = models.CharField(max_length=40)          # a registered kind, e.g. 'shop.product'
    object_id = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    summary = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)   # proposed values (update)
    before = models.JSONField(default=dict, blank=True)    # the live values when it was sent
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='change_requests')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='reviewed_change_requests')
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'change_requests'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'target_type']),
                   models.Index(fields=['target_type', 'object_id'])]

    def __str__(self):
        return f'{self.get_kind_display()} {self.target_type}#{self.object_id} ({self.status})'


class StaffActivity(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                              related_name='staff_activity')
    role = models.CharField(max_length=20, blank=True)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=250)
    status_code = models.PositiveSmallIntegerField()
    action = models.CharField(max_length=120, blank=True)    # readable: "Approved signup", "Edited product"
    area = models.CharField(max_length=40, blank=True)       # registrations, shop, coach, approvals…
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'staff_activity'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['actor', 'created_at'])]
