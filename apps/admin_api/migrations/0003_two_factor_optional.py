"""2-step verification becomes optional for staff: switch the "required for staff and coaches" setting off.

An admin can switch it back on in Admin → Settings → Sign-in security.
"""
from django.db import migrations


def make_optional(apps, schema_editor):
    SystemSettings = apps.get_model('admin_api', 'SystemSettings')
    for s in SystemSettings.objects.all():
        sec = dict(s.security or {})
        sec['two_factor_required_for_staff'] = False
        s.security = sec
        s.save(update_fields=['security'])


class Migration(migrations.Migration):

    dependencies = [
        ('admin_api', '0002_adminseen'),
    ]

    operations = [
        migrations.RunPython(make_optional, migrations.RunPython.noop),
    ]
