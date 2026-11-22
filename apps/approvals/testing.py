"""Test helper: an admin approves everything waiting (reviews etc.), as if they'd clicked Approve on each."""


def approve_pending():
    from django.contrib.auth import get_user_model

    from .models import ChangeRequest
    from .registry import approve
    User = get_user_model()
    admin = (User.objects.filter(role='admin').first()
             or User.objects.create_user(username='approver', email='approver@t.local', password='x', role='admin'))
    for cr in ChangeRequest.objects.filter(status='pending'):
        approve(cr, admin)
