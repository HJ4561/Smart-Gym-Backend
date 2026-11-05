"""
What still has no Arabic: every record whose English text is filled in but its Arabic twin (`<field>_ar`,
or `ar` for the website's texts and lists) is empty — so staff can work through them.

Used by `python manage.py list_missing_arabic` and GET /api/admin/missing-arabic/ (the admin portal's
"Missing Arabic" page). Fields are found automatically: any `x` with an `x_ar` next to it.
"""
from django.apps import apps
from django.conf import settings

# (key, English label, Arabic label, model, name of the record, admin portal link or None for Django admin)
SOURCES = [
    ('products', 'Shop products', 'منتجات المتجر', 'shop.Product', lambda o: o.name, '/admin/shop?highlight={id}'),
    ('categories', 'Shop categories', 'فئات المتجر', 'shop.ProductCategory', lambda o: o.name, '/admin/shop'),
    ('classes', 'Classes', 'الحصص', 'classes.GymClass', lambda o: o.name, '/admin/classes?highlight={id}'),
    ('courses', 'Courses', 'الدورات', 'catalog.Course', lambda o: o.title, '/admin/trainers?tab=courses'),
    ('lessons', 'Course lessons', 'دروس الدورات', 'catalog.CourseModule',
     lambda o: f'{o.course.title} — {o.title}', '/admin/trainers?tab=courses'),
    ('trainers', 'Trainers', 'المدربون', 'catalog.TrainerProfile',
     lambda o: o.user.full_name or o.user.email, '/admin/trainers?highlight={id}'),
    ('nutritionists', 'Nutritionists', 'أخصائيو التغذية', 'nutrition.NutritionistProfile',
     lambda o: o.user.full_name or o.user.email, '/admin/nutrition'),
    ('machines', 'Machines', 'الأجهزة', 'workouts.Exercise', lambda o: o.name, '/admin/machines?highlight={id}'),
    ('videos', 'Machine videos', 'فيديوهات الأجهزة', 'workouts.ExerciseVideo',
     lambda o: f'{o.exercise.name} — {o.title}', '/admin/machines?highlight={exercise_id}'),
    ('plans', 'Membership plans', 'خطط العضوية', 'members.MembershipPlan', lambda o: o.display_name,
     '/admin/site-plans?highlight={id}'),
    ('milestones', 'Attendance milestones', 'إنجازات الحضور', 'attendance.Milestone', lambda o: o.title, None),
    ('templates', 'Workout templates', 'قوالب التمارين', 'workouts.WorkoutTemplate', lambda o: o.name, None),
    ('articles', 'Articles (older list)', 'المقالات (القائمة القديمة)', 'cms.Article', lambda o: o.title, None),
    ('facilities', 'Facilities (older list)', 'المرافق (القائمة القديمة)', 'facilities.Facility', lambda o: o.name, None),
]


def _filled(value):
    if isinstance(value, (list, tuple)):
        return any(str(v).strip() for v in value)
    return bool(str(value or '').strip())


def _pairs(model):
    names = {f.name for f in model._meta.get_fields() if getattr(f, 'concrete', False)}
    return [n for n in sorted(names) if f'{n}_ar' in names and not n.endswith('_ar')]


def _django_admin(model, pk):
    base = (getattr(settings, 'BACKEND_PUBLIC_URL', '') or '').rstrip('/')
    return f'{base}/django-admin/{model._meta.app_label}/{model._meta.model_name}/{pk}/change/'


def _records(key, label, label_ar, model_path, name_of, link):
    model = apps.get_model(model_path)
    fields = _pairs(model)
    items = []
    for obj in model.objects.all().order_by('pk'):
        missing = [f for f in fields if _filled(getattr(obj, f)) and not _filled(getattr(obj, f'{f}_ar'))]
        if not missing:
            continue
        try:
            name = name_of(obj)
        except Exception:   # noqa: BLE001 — a broken relation shouldn't hide the record
            name = f'#{obj.pk}'
        row = {'id': obj.pk, 'name': name, 'fields': missing, 'link': '', 'edit_url': ''}
        if link:
            row['link'] = link.format(id=obj.pk, exercise_id=getattr(obj, 'exercise_id', ''))
        else:
            row['edit_url'] = _django_admin(model, obj.pk)
        items.append(row)
    return {'key': key, 'label': label, 'label_ar': label_ar, 'fields': fields, 'count': len(items), 'items': items}


def _site_texts():
    from apps.site_content.models import SiteText
    items = [{'id': t.key, 'name': t.key, 'fields': ['text'], 'link': '/admin/site-content', 'edit_url': '',
              'page': t.page}
             for t in SiteText.objects.order_by('page', 'key') if _filled(t.en) and not _filled(t.ar)]
    return {'key': 'site_texts', 'label': 'Website texts', 'label_ar': 'نصوص الموقع', 'fields': ['text'],
            'count': len(items), 'items': items}


def _site_lists():
    from apps.site_content.models import SiteItem
    items = []
    for item in SiteItem.objects.order_by('collection', 'order', 'pk'):
        data = item.data or {}
        missing = [k for k, v in data.items()
                   if isinstance(v, dict) and set(v) & {'en', 'ar'} and _filled(v.get('en')) and not _filled(v.get('ar'))]
        if missing:
            title = data.get('title') or data.get('name') or {}
            name = (title.get('en') if isinstance(title, dict) else str(title)) or f'#{item.pk}'
            items.append({'id': item.pk, 'name': f'{item.collection}: {name}', 'fields': missing,
                          'link': f'/admin/site-lists?list={item.collection}&edit={item.pk}', 'edit_url': ''})
    return {'key': 'site_lists', 'label': 'Website lists', 'label_ar': 'قوائم الموقع', 'fields': [],
            'count': len(items), 'items': items}


def missing_arabic():
    """[{key, label, label_ar, fields, count, items: [{id, name, fields, link | edit_url}]}]."""
    return [_records(*src) for src in SOURCES] + [_site_lists(), _site_texts()]
