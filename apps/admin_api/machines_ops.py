"""
Admin → Machines & QR. A machine is a workouts.Exercise with a QR tag; members scan the tag
(or the printed link /dashboard/machine-guide?qr=TAG) to open its guide and log sets.
"""
import logging
import secrets
import re
from decimal import Decimal, InvalidOperation

from django.db.models import Count, ProtectedError, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.core.i18n import read_ar, with_ar
from apps.workouts.models import Exercise

from .permissions import IsAdminUser

logger = logging.getLogger(__name__)

CATEGORIES = dict(Exercise.CATEGORY_CHOICES)
MUSCLES = dict(Exercise.MUSCLE_CHOICES)
URL_FIELDS = ('video_url', 'image_url')
TEXT_FIELDS = {'equipment_type': 60, 'machine_id': 20, 'description': 4000, 'setup_instructions': 4000,
               'target_intensity': 60, 'cadence': 60}


def new_tag():
    """SG-MCH-XXXXXX, unique among machines (case-insensitive)."""
    while True:
        tag = f'SG-MCH-{secrets.token_hex(3).upper()}'
        if not Exercise.objects.filter(qr_tag_id__iexact=tag).exists():
            return tag


def _row(ex):
    return {
        'id': ex.id, 'name': ex.name, 'category': ex.category, 'category_label': CATEGORIES.get(ex.category, ex.category),
        'primary_muscle': ex.primary_muscle, 'muscle_label': MUSCLES.get(ex.primary_muscle, ex.primary_muscle),
        'equipment_type': ex.equipment_type, 'machine_id': ex.machine_id, 'qr_tag_id': ex.qr_tag_id,
        'description': ex.description, 'setup_instructions': ex.setup_instructions,
        **with_ar(ex, 'name', 'description', 'setup_instructions'),
        'technique_cues': ex.technique_cues or [], 'video_url': ex.video_url, 'image_url': ex.image_url,
        'default_rest_seconds': ex.default_rest_seconds,
        'recommended_weight_kg': float(ex.recommended_weight_kg) if ex.recommended_weight_kg is not None else None,
        'target_intensity': ex.target_intensity, 'cadence': ex.cadence, 'is_compound': ex.is_compound,
        'is_active': ex.is_active,
        'total_workout_entries': getattr(ex, 'entries', None) if getattr(ex, 'entries', None) is not None
        else ex.workout_entries.count(),
        'entries_30d': getattr(ex, 'entries_30d', None),
    }


def _apply(ex, data, errors):
    if 'name' in data:
        name = str(data.get('name') or '').strip()
        if not name:
            errors['name'] = 'Required.'
        elif not 3 <= len(name) <= 60:
            errors['name'] = 'Machine name must be 3–60 characters.'
        elif Exercise.objects.filter(name__iexact=name).exclude(id=ex.id).exists():
            errors['name'] = 'Another machine already has this name.'
        ex.name = name
    if 'category' in data:
        if data.get('category') not in CATEGORIES:
            errors['category'] = 'Choose a category.'
        ex.category = data.get('category')
    if 'primary_muscle' in data:
        if data.get('primary_muscle') not in MUSCLES:
            errors['primary_muscle'] = 'Choose a muscle group.'
        ex.primary_muscle = data.get('primary_muscle')
    for f, n in TEXT_FIELDS.items():
        if f in data:
            v = str(data.get(f) or '').strip()
            if len(v) > n:
                errors[f] = f'Keep it under {n} characters.'
            setattr(ex, f, v)
    read_ar(ex, data, {'name': 60, 'description': 4000, 'setup_instructions': 4000}, errors)
    if 'machine_id' in data:
        # Floor number / asset ID: 2–4 letters, a dash and 2–3 digits ("ST-07", "CARD-112").
        v = str(data.get('machine_id') or '').strip().upper()
        if v and not re.fullmatch(r'[A-Z]{2,4}-\d{2,3}', v):
            errors['machine_id'] = 'Use 2–4 letters, a dash and 2–3 digits, e.g. ST-07 or CARD-112.'
        elif v and Exercise.objects.filter(machine_id__iexact=v).exclude(id=ex.id).exists():
            errors['machine_id'] = f'{v} is already used by another machine.'
        ex.machine_id = v
    for f in URL_FIELDS:
        if f in data:
            v = str(data.get(f) or '').strip()
            if v and not v.startswith(('https://', 'http://')):
                errors[f] = 'Enter a full link (https://…).'
            setattr(ex, f, v[:200])
    if 'technique_cues' in data:
        raw = data.get('technique_cues') or []
        if isinstance(raw, str):
            raw = raw.splitlines()
        ex.technique_cues = [str(c).strip()[:200] for c in raw if str(c).strip()][:12]
    if 'default_rest_seconds' in data:
        try:
            n = int(data.get('default_rest_seconds'))
            if not 0 <= n <= 900:
                raise ValueError
            ex.default_rest_seconds = n
        except (TypeError, ValueError):
            errors['default_rest_seconds'] = 'Enter seconds from 0 to 900.'
    if 'recommended_weight_kg' in data:
        v = data.get('recommended_weight_kg')
        if v in (None, ''):
            ex.recommended_weight_kg = None
        else:
            try:
                d = Decimal(str(v)).quantize(Decimal('0.01'))
                if not Decimal('0') <= d <= Decimal('1000'):
                    raise InvalidOperation
                ex.recommended_weight_kg = d
            except (InvalidOperation, ValueError):
                errors['recommended_weight_kg'] = 'Enter kilograms from 0 to 1000.'
    for flag in ('is_active', 'is_compound'):
        if flag in data:
            setattr(ex, flag, bool(data.get(flag)))


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def machines(request):
    """
    GET  /api/admin/machines/?category=&q=&status=active|offline|untagged
    POST /api/admin/machines/ {name, category, primary_muscle, …} — gets a QR tag straight away.
    """
    if request.method == 'GET':
        since = timezone.now() - timezone.timedelta(days=30)
        qs = Exercise.objects.annotate(
            entries=Count('workout_entries', distinct=True),
            entries_30d=Count('workout_entries', filter=Q(workout_entries__workout__started_at__gte=since), distinct=True),
        ).order_by('-is_active', 'category', 'name')
        if request.query_params.get('category') in CATEGORIES:
            qs = qs.filter(category=request.query_params['category'])
        st = request.query_params.get('status')
        if st == 'active':
            qs = qs.filter(is_active=True)
        elif st == 'offline':
            qs = qs.filter(is_active=False)
        elif st == 'untagged':
            qs = qs.filter(Q(qr_tag_id='') | Q(qr_tag_id__isnull=True))
        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(qr_tag_id__icontains=q) | Q(equipment_type__icontains=q)
                           | Q(machine_id__icontains=q))
        return Response([_row(ex) for ex in qs])

    ex = Exercise(is_active=True)
    errors = {}
    _apply(ex, {'name': '', 'category': '', 'primary_muscle': '', **request.data}, errors)
    if errors:
        return Response(errors, status=400)
    ex.qr_tag_id = new_tag()
    ex.save()
    logger.info('Machine %s (%s) added by %s', ex.name, ex.qr_tag_id, request.user.email)
    return Response(_row(ex), status=201)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def machine_update(request, exercise_id):
    """
    PATCH  /api/admin/machines/<id>/ — any field; is_active=false takes it offline (scans stop working).
    DELETE — only a machine no member has logged a set on (their history would point at it).
    """
    ex = get_object_or_404(Exercise, id=exercise_id)
    if request.method == 'DELETE':
        used = ex.workout_entries.count()
        if used:
            return Response({'detail': f'{ex.name} appears in {used} logged workout{"s" if used != 1 else ""}, '
                                       'so it can’t be deleted — take it offline instead.'}, status=400)
        try:
            ex.delete()
        except ProtectedError:
            return Response({'detail': f'{ex.name} is part of a workout template, so it can’t be deleted — '
                                       'take it offline instead.'}, status=400)
        logger.info('Machine %s (%s) deleted by %s', ex.name, ex.qr_tag_id, request.user.email)
        return Response(status=204)
    errors = {}
    _apply(ex, request.data, errors)
    if errors:
        return Response(errors, status=400)
    ex.save()
    return Response(_row(ex))


@api_view(['POST'])
@permission_classes([IsAdminUser])
def regenerate_qr(request, exercise_id):
    """POST /api/admin/machines/<id>/regenerate-qr/ — new tag; the old printed QR stops working."""
    ex = get_object_or_404(Exercise, id=exercise_id)
    old = ex.qr_tag_id
    ex.qr_tag_id = new_tag()
    ex.save(update_fields=['qr_tag_id'])
    logger.info('QR of %s changed %s → %s by %s', ex.name, old or '-', ex.qr_tag_id, request.user.email)
    return Response(_row(ex))


@api_view(['GET'])
@permission_classes([IsAdminUser])
def machine_stats(request):
    """GET /api/admin/machines/stats/"""
    since = timezone.now() - timezone.timedelta(days=30)
    active = Exercise.objects.filter(is_active=True)
    top = (active.annotate(n=Count('workout_entries', filter=Q(workout_entries__workout__started_at__gte=since)))
           .filter(n__gt=0).order_by('-n').first())
    untagged = active.filter(Q(qr_tag_id='') | Q(qr_tag_id__isnull=True)).count()
    return Response({
        'total': active.count(),
        'offline': Exercise.objects.filter(is_active=False).count(),
        'with_qr': active.count() - untagged,
        'without_qr': untagged,
        'top_machine': top.name if top else None,
        'top_machine_count': top.n if top else 0,
    })


@api_view(['GET'])
@permission_classes([IsAdminUser])
def machine_categories(request):
    """GET /api/admin/machines/categories/ → [{value, label}] plus muscle groups for the editor."""
    return Response({
        'categories': [{'value': k, 'label': v} for k, v in Exercise.CATEGORY_CHOICES],
        'muscles': [{'value': k, 'label': v} for k, v in Exercise.MUSCLE_CHOICES],
    })


# ============================================================
# MACHINE VIDEOS (coaches add them; the gym can add, edit and hide any)
# ============================================================
@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def machine_videos(request, exercise_id):
    """GET /api/admin/machines/<id>/videos/ · POST {tier, title, video_url, duration_minutes, price, coach_id?}"""
    from apps.catalog.models import TrainerProfile
    from apps.workouts import videos as vids
    from apps.workouts.models import ExerciseVideo
    ex = get_object_or_404(Exercise, id=exercise_id)
    if request.method == 'GET':
        qs = vids.with_sales(ExerciseVideo.objects.filter(exercise=ex)).order_by('created_at')
        return Response([vids.coach_row(v) for v in qs])
    errors = {}
    coach = None
    if request.data.get('coach_id'):
        coach = TrainerProfile.objects.filter(id=request.data['coach_id']).first()
        if coach is None:
            errors['coach_id'] = 'Choose a coach.'
    v = ExerciseVideo(exercise=ex, coach=coach, tier='basic')
    vids.apply(v, {'title': '', 'video_url': '', 'tier': 'basic', 'price': 0, **request.data}, errors)
    if errors:
        return Response(errors, status=400)
    v.save()
    return Response(vids.coach_row(v), status=201)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def machine_video_detail(request, video_id):
    """PATCH /api/admin/machine-videos/<id>/ any field · DELETE when nobody bought it (hide it otherwise)."""
    from apps.workouts import videos as vids
    from apps.workouts.models import ExerciseVideo
    v = get_object_or_404(ExerciseVideo.objects.select_related('exercise', 'coach__user'), id=video_id)
    if request.method == 'DELETE':
        if v.purchases.exists():
            return Response({'detail': 'Members have bought this video — hide it instead, so they keep access.'}, status=400)
        v.delete()
        return Response(status=204)
    errors = {}
    if v.purchases.exists() and 'tier' in request.data and request.data['tier'] != v.tier:
        errors['tier'] = 'Members bought this video, so its tier can’t change.'
    if not errors:
        vids.apply(v, request.data, errors)
    if errors:
        return Response(errors, status=400)
    v.save()
    return Response(vids.coach_row(v))
