"""Arabic for the three seeded milestones (matched by reward code; ones an admin changed keep theirs)."""
from django.db import migrations

ARABIC = {
    'CONSISTENT-15': {
        'tier_ar': 'المستوى 01 • البداية', 'title_ar': 'الالتزام 15 يومًا',
        'description_ar': 'قسيمة: خصم 15% على مكملات الأداء والملابس في متجر النادي.',
        'reward_value_ar': 'قسيمة خصم 15%',
    },
    'CRYO-30': {
        'tier_ar': 'المستوى 02 • الوحش', 'title_ar': '30 يومًا بلا توقف',
        'description_ar': 'جلسة مجانية في غرف التبريد العميق (بقيمة 65 دولارًا).',
        'reward_value_ar': 'جلسة تبريد بقيمة 65 دولارًا',
    },
    'CENTURY-100': {
        'tier_ar': 'المستوى 03 • نادي المئة', 'title_ar': 'نادي المئة يوم',
        'description_ar': 'طوق بار تيتانيوم أسود محفور + خزانة VIP لمدة عام.',
        'reward_value_ar': 'طوق تيتانيوم + خزانة VIP',
    },
}


def add_arabic(apps, schema_editor):
    Milestone = apps.get_model('attendance', 'Milestone')
    for code, fields in ARABIC.items():
        Milestone.objects.filter(reward_code=code, title_ar='').update(**fields)


class Migration(migrations.Migration):
    dependencies = [('attendance', '0002_milestone_description_ar_milestone_reward_value_ar_and_more')]
    operations = [migrations.RunPython(add_arabic, migrations.RunPython.noop)]
