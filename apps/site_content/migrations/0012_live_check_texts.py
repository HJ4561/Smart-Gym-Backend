"""
Camera texts for the live face check at signup (look straight, turn to each side, hints) and
"a photo of a photo isn't accepted". Admin-edited texts are kept.
"""
from django.db import migrations


def forward(apps, schema_editor):
    from apps.site_content.seeding import seed

    seed(apps.get_model('site_content', 'SiteText'), apps.get_model('site_content', 'SiteImage'))


class Migration(migrations.Migration):

    dependencies = [
        ('site_content', '0011_face_check_texts'),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
