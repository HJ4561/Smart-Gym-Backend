"""Every kind of content that waits for an admin. Each app's kinds are registered here."""
from .registry import register


# ---------------------------------------------------------------- member reviews
def _review_kind(key, label, label_ar, model_path, what, refresh, owner_path, admin_path):
    from django.apps import apps
    from apps.catalog.reviews import check_review
    model = apps.get_model(model_path)

    def describe(r):
        who = r.member.full_name or r.member.email
        return f'{r.rating}★ review of {what(r)} by {who}'

    def snapshot(r):
        return {'rating': r.rating, 'comment': r.comment, 'member': r.member.full_name or r.member.email,
                'about': what(r)}

    def apply(r, data):
        values, errors = check_review(data, partial=True)
        for f, v in values.items():
            setattr(r, f, v)
        return errors

    def publish(r):
        r.status = 'approved'
        r.save()
        refresh(r)

    def hide(r):
        r.status = 'rejected'
        r.save(update_fields=['status'])
        refresh(r)

    def remove(r):
        r.delete()
        refresh(r)

    register(key, label=label, label_ar=label_ar, model=model, describe=describe, snapshot=snapshot,
             apply=apply, publish=publish, hide=hide, remove=remove, owner_path=owner_path, admin_path=admin_path,
             editable=('rating', 'comment'))


def _course_refresh(r):
    from apps.catalog.reviews import refresh_course_rating
    refresh_course_rating(r.course)


def _trainer_refresh(r):
    from apps.catalog.reviews import refresh_trainer_rating
    refresh_trainer_rating(r.trainer)


_review_kind('catalog.course_review', 'Course review', 'تقييم دورة', 'catalog.CourseReview',
             lambda r: r.course.title, _course_refresh,
             lambda r: f'/dashboard/courses/{r.course_id}', '/admin/approvals')
_review_kind('catalog.trainer_review', 'Coach review', 'تقييم مدرب', 'catalog.TrainerReview',
             lambda r: r.trainer.user.full_name if r.trainer.user_id else 'a coach', _trainer_refresh,
             lambda r: f'/dashboard/trainers/{r.trainer_id}', '/admin/approvals')


# ---------------------------------------------------------------- shop (sent by the shop manager)
def _errors_of(fn):
    """shop_ops' apply(obj, data, errors) → the registry's apply(obj, data) → errors."""
    def apply(obj, data):
        errors = {}
        fn(obj, data, errors)
        return errors
    return apply


def _shop_kinds():
    from decimal import Decimal

    from django.db.models import ProtectedError

    from apps.admin_api import shop_ops
    from apps.shop.models import OrderItem, Product, ProductCategory, PromoCode

    def plain(v):
        return float(v) if isinstance(v, Decimal) else v

    def publish(obj):
        obj.approval_status = 'approved'
        obj.save()

    def hide(obj):
        obj.approval_status = 'rejected'
        obj.save(update_fields=['approval_status'])

    product_fields = ('name', 'name_ar', 'category_id', 'sku', 'price', 'original_price', 'description',
                      'description_ar', 'price_note', 'price_note_ar', 'badge_top', 'badge_top_ar', 'badge_secondary',
                      'badge_secondary_ar', 'telemetry', 'telemetry_ar', 'image_url', 'is_active', 'is_featured')

    def remove_product(p):
        if OrderItem.objects.filter(product=p).exists():
            p.is_active = False          # ordered before: hidden instead, so old orders keep their line
            p.save(update_fields=['is_active', 'updated_at'])
        else:
            p.delete()

    register('shop.product', label='Shop product', label_ar='منتج في المتجر', model=Product,
             describe=lambda p: f'{p.name} — SAR {p.price}',
             snapshot=lambda p: {f: plain(getattr(p, f)) for f in product_fields},
             apply=_errors_of(shop_ops._apply_product), publish=publish, hide=hide, remove=remove_product,
             owner_path='/shop/products', admin_path='/admin/shop',
             editable=('name', 'name_ar', 'price', 'original_price', 'description', 'description_ar', 'price_note',
                       'badge_top', 'badge_secondary', 'is_active', 'is_featured'))

    def remove_category(c):
        try:
            c.delete()
        except ProtectedError:
            c.is_active = False
            c.save(update_fields=['is_active'])

    category_fields = ('name', 'name_ar', 'icon', 'description', 'description_ar', 'display_order', 'is_active')
    register('shop.category', label='Shop category', label_ar='فئة في المتجر', model=ProductCategory,
             describe=lambda c: c.name, snapshot=lambda c: {f: getattr(c, f) for f in category_fields},
             apply=_errors_of(shop_ops._apply_category), publish=publish, hide=hide, remove=remove_category,
             owner_path='/shop/categories', admin_path='/admin/shop',
             editable=('name', 'name_ar', 'description', 'description_ar', 'is_active'))

    def remove_promo(p):
        if p.used_count:
            p.is_active = False
            p.save(update_fields=['is_active'])
        else:
            p.delete()

    promo_fields = ('code', 'description', 'discount_percent', 'max_uses', 'valid_from', 'valid_until', 'is_active')
    register('shop.promo', label='Promo code', label_ar='كود خصم', model=PromoCode,
             describe=lambda p: f'{p.code} (−{plain(p.discount_percent):g}%)',
             snapshot=lambda p: {f: plain(getattr(p, f)) for f in promo_fields},
             apply=_errors_of(shop_ops._apply_promo), publish=publish, hide=hide, remove=remove_promo,
             owner_path='/shop/promos', admin_path='/admin/shop',
             editable=('description', 'discount_percent', 'max_uses', 'valid_until', 'is_active'))


_shop_kinds()


# ---------------------------------------------------------------- nutritionist profile (what members read)
def _nutrition_kinds():
    from apps.admin_api.catalog_ops import _apply_trainer
    from apps.nutrition.models import NutritionistProfile
    from apps.nutrition.portal import PROFILE_FIELDS

    def apply(n, data):
        errors = {}
        safe = {k: v for k, v in data.items() if k in PROFILE_FIELDS}
        _apply_trainer(n, n.user, {k: v for k, v in safe.items() if k != 'photo_url'}, errors)
        if 'photo_url' in safe:
            url = str(safe.get('photo_url') or '')
            if len(url) > 500 or (url and not url.startswith(('/', 'http://', 'https://'))):
                errors['photo_url'] = 'Upload the photo again.'
            else:
                n.photo_url = url
        return errors

    def snapshot(n):
        out = {f: getattr(n, f) for f in PROFILE_FIELDS}
        out['hourly_rate'] = float(n.hourly_rate) if n.hourly_rate is not None else None
        return out

    register('nutrition.profile', label='Nutritionist profile', label_ar='ملف أخصائي التغذية', model=NutritionistProfile,
             describe=lambda n: n.user.full_name or n.user.email, snapshot=snapshot, apply=apply,
             owner_path='/nutrition/profile', admin_path='/admin/nutrition',
             editable=('title', 'title_ar', 'credentials', 'credentials_ar', 'bio', 'bio_ar', 'specialties',
                       'years_experience', 'hourly_rate'))


_nutrition_kinds()


# ---------------------------------------------------------------- coach uploads
def _coach_kinds():
    from apps.admin_api import catalog_ops
    from apps.catalog.models import Course
    from apps.classes.models import ClassPost
    from apps.workouts import videos as vids
    from apps.workouts.models import ExerciseVideo

    def publish(obj):
        obj.approval_status = 'approved'
        obj.save()

    def hide(obj):
        obj.approval_status = 'rejected'
        obj.save(update_fields=['approval_status'])

    # Machine Guide videos
    video_fields = ('title', 'title_ar', 'description', 'description_ar', 'tier', 'price', 'video_url',
                    'duration_minutes', 'thumbnail_url', 'is_active')

    def remove_video(v):
        if v.purchases.exists():
            v.is_active = False
            v.save(update_fields=['is_active', 'updated_at'])
        else:
            v.delete()

    register('coach.video', label='Coach video', label_ar='فيديو مدرب', model=ExerciseVideo,
             describe=lambda v: f'{v.title} — {v.exercise.name}',
             snapshot=lambda v: {f: (float(getattr(v, f)) if f == 'price' else getattr(v, f)) for f in video_fields},
             apply=_errors_of(vids.apply), publish=publish, hide=hide, remove=remove_video,
             owner_path='/coach/videos', admin_path='/admin/machines',
             editable=('title', 'title_ar', 'description', 'description_ar', 'tier', 'price', 'duration_minutes'))

    # Changes to a live course (new courses keep their own review on the Coaches page)
    course_fields = ('title', 'title_ar', 'subtitle', 'subtitle_ar', 'description', 'description_ar', 'category',
                     'level', 'duration_weeks', 'days_per_week', 'estimated_minutes_per_session', 'price',
                     'promo_video_url')

    def lessons(raw):
        out = []
        for m in raw or []:
            get = (lambda k, d=None: m.get(k, d)) if isinstance(m, dict) else (lambda k, d=None: getattr(m, k, d))
            out.append({'title': str(get('title', '') or '').strip(), 'video_url': str(get('video_url', '') or ''),
                        'week': int(get('week_number', 1) or 1), 'day': int(get('day_number', 1) or 1),
                        'minutes': int(get('duration_minutes', 1) or 1),
                        'description': str(get('description', '') or '').strip()})
        return out

    def course_snapshot(c):
        row = {f: (float(getattr(c, f)) if f == 'price' else getattr(c, f)) for f in course_fields}
        new = getattr(c, '_new_modules', None)
        row['lessons'] = lessons(new) if new is not None else lessons(c.modules.order_by('order')) if c.pk else []
        return row

    def course_apply(c, data):
        errors = {}
        catalog_ops._apply_course(c, {k: v for k, v in data.items() if k in course_fields}, errors)
        if 'modules' in data:
            if not data['modules']:
                errors['modules'] = 'A published course needs at least one lesson.'
            else:
                catalog_ops._replace_modules(c, data['modules'], errors, dry_run=True)
                c._new_modules = data['modules']
        return errors

    def course_save(c):
        c.save()
        if getattr(c, '_new_modules', None) is not None:
            errors = {}
            catalog_ops._replace_modules(c, c._new_modules, errors)
            if errors:
                from .registry import ReviewError
                raise ReviewError(errors)

    register('coach.course', label='Course change', label_ar='تعديل دورة', model=Course,
             describe=lambda c: c.title, snapshot=course_snapshot, apply=course_apply, save=course_save,
             owner_path=lambda c: f'/coach/courses/{c.id}', admin_path='/admin/trainers?tab=courses',
             editable=('title', 'title_ar', 'subtitle', 'description', 'description_ar', 'price'))

    # Class videos
    def post_apply(p, data):
        errors = {}
        if 'title' in data:
            p.title = ' '.join(str(data.get('title') or '').split())
            if not 3 <= len(p.title) <= 120:
                errors['title'] = 'Title must be 3–120 characters.'
        if 'body' in data:
            p.body = str(data.get('body') or '').strip()[:2000]
        if 'video_url' in data:
            p.video_url = str(data.get('video_url') or '').strip()
            if not p.video_url.startswith('https://') or len(p.video_url) > 300:
                errors['video_url'] = 'Paste the full video link (https://…, e.g. YouTube or Vimeo).'
        return errors

    def post_publish(p):
        publish(p)
        from apps.coach.extra import tell_class
        tell_class(p)

    register('coach.class_video', label='Class video', label_ar='فيديو حصة', model=ClassPost,
             describe=lambda p: f'{p.title} — {p.gym_class.name}',
             snapshot=lambda p: {'class': p.gym_class.name, 'for_date': p.for_date, 'title': p.title, 'body': p.body,
                                 'video_url': p.video_url, 'thumbnail_url': p.thumbnail_url},
             apply=post_apply, publish=post_publish, hide=hide, remove=lambda p: p.delete(),
             owner_path='/coach/classes', admin_path='/admin/classes', editable=('title', 'body', 'video_url'))


_coach_kinds()


# ---------------------------------------------------------------- coach profile (what members read)
def _coach_profile_kind():
    from apps.admin_api.catalog_ops import _apply_trainer
    from apps.catalog.models import TrainerProfile
    from apps.coach.views import COACH_PUBLIC

    def apply(t, data):
        errors = {}
        safe = {k: v for k, v in data.items() if k in COACH_PUBLIC}
        _apply_trainer(t, t.user, safe, errors)
        if 'photo_url' in data:
            url = str(data.get('photo_url') or '')
            if len(url) > 500 or (url and not url.startswith(('/', 'http://', 'https://'))):
                errors['photo_url'] = 'Upload the photo again.'
            else:
                t.photo_url = url
        return errors

    def snapshot(t):
        row = {'first_name': t.user.first_name, 'last_name': t.user.last_name}
        row.update({f: getattr(t, f) for f in COACH_PUBLIC if f not in ('first_name', 'last_name')})
        row['photo_url'] = t.photo_url
        return row

    def save(t):
        t.user.save()          # the name lives on the account
        t.save()

    register('coach.profile', label='Coach profile', label_ar='ملف المدرب', model=TrainerProfile,
             describe=lambda t: t.user.full_name or t.user.email, snapshot=snapshot, apply=apply, save=save,
             owner_path='/coach/profile', admin_path='/admin/trainers',
             editable=('first_name', 'last_name', 'title', 'title_ar', 'credentials', 'credentials_ar', 'bio', 'bio_ar',
                       'specialties', 'years_experience'))


_coach_profile_kind()
