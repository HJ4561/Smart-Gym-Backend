"""
Machine Guide videos in three tiers. Basic videos are free for every member. Advanced and Premium
videos are included with some membership plans (MembershipPlan.video_access) or bought one by one
(VideoPurchase, through the payments gateway, purpose "video"). A locked video never sends its link.

Coaches add videos for any machine from the coach portal; the gym can too (admin Machines page).
"""
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Sum

from apps.core.i18n import read_ar, with_ar

from .models import ExerciseVideo, VideoPurchase

RANK = ExerciseVideo.TIER_RANK
TIERS = dict(ExerciseVideo.TIER_CHOICES)
PRICE_MAX = Decimal('1000')


def plan_level(member):
    """(rank, plan name) of the videos the member's plan includes — 0 (basic only) without a valid membership."""
    from apps.members.models import Membership
    m = Membership.objects.select_related('plan').filter(member=member).first()
    if m is None or not m.allows_entry:
        return 0, ''
    return RANK.get(m.plan.video_access, 0), m.plan.display_name


def access(member, video, level=None, purchased=None):
    """(can watch, why) — why is 'free' | 'plan' | 'bought' | 'locked'."""
    if video.tier == 'basic':
        return True, 'free'
    if level is None:
        level, _ = plan_level(member)
    if level >= RANK[video.tier]:
        return True, 'plan'
    if purchased is None:
        purchased = set(VideoPurchase.objects.filter(member=member).values_list('video_id', flat=True))
    if video.id in purchased:
        return True, 'bought'
    return False, 'locked'


def plans_including(tier):
    """Display names of the active plans whose members get this tier, for "Included with …" hints."""
    from apps.members.models import MembershipPlan
    ok = [k for k, r in RANK.items() if r >= RANK[tier]]
    return list(MembershipPlan.objects.filter(is_active=True, video_access__in=ok).values_list('display_name', flat=True))


def member_row(v, can, why, included_in):
    coach = v.coach.user.full_name if v.coach_id else 'Smart Gym'
    return {
        'id': v.id, 'tier': v.tier, 'tier_label': TIERS[v.tier], 'title': v.title, 'description': v.description,
        **with_ar(v, 'title', 'description'),
        'duration_minutes': v.duration_minutes, 'price': str(v.price), 'coach': coach,
        'locked': not can, 'access': why,
        'video_url': v.video_url if can else None,          # never sent while locked
        'included_in': included_in if v.tier != 'basic' else [],
    }


def coach_row(v):
    agg = getattr(v, 'sold', None)
    return {
        'id': v.id, 'exercise_id': v.exercise_id, 'exercise': v.exercise.name, 'exercise_ar': v.exercise.name_ar,
        'machine_id': v.exercise.machine_id,
        'tier': v.tier, 'tier_label': TIERS[v.tier], 'title': v.title, 'description': v.description,
        **with_ar(v, 'title', 'description'),
        'video_url': v.video_url, 'duration_minutes': v.duration_minutes, 'price': str(v.price),
        'is_active': v.is_active, 'coach_id': v.coach_id,
        'coach': v.coach.user.full_name if v.coach_id else 'Smart Gym',
        'purchases': agg if agg is not None else v.purchases.count(),
        'revenue': str(getattr(v, 'revenue', None) or v.purchases.aggregate(t=Sum('amount'))['t'] or 0),
        'created_at': v.created_at,
    }


def with_sales(qs):
    return qs.select_related('exercise', 'coach__user').annotate(sold=Count('purchases'), revenue=Sum('purchases__amount'))


def apply(v, data, errors):
    """Validates and copies editable fields onto a video (not saved)."""
    if 'title' in data:
        v.title = ' '.join(str(data.get('title') or '').split())
        if not 3 <= len(v.title) <= 120:
            errors['title'] = 'Title must be 3–120 characters.'
    if 'description' in data:
        v.description = str(data.get('description') or '').strip()[:2000]
    read_ar(v, data, {'title': 120, 'description': 2000}, errors)
    if 'video_url' in data:
        v.video_url = str(data.get('video_url') or '').strip()
        if not v.video_url.startswith('https://') or len(v.video_url) > 200:
            errors['video_url'] = 'Paste the full video link (https://…, e.g. YouTube or Vimeo).'
    if 'tier' in data:
        if data.get('tier') not in TIERS:
            errors['tier'] = 'Choose Basic, Advanced or Premium.'
        else:
            v.tier = data['tier']
    if 'duration_minutes' in data:
        try:
            n = int(data.get('duration_minutes'))
            if not 1 <= n <= 180:
                raise ValueError
            v.duration_minutes = n
        except (TypeError, ValueError):
            errors['duration_minutes'] = 'Length must be 1–180 minutes.'
    if 'price' in data:
        try:
            v.price = Decimal(str(data.get('price') or '0')).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError):
            errors['price'] = 'Enter a price in SAR.'
    if 'is_active' in data:
        v.is_active = bool(data.get('is_active'))
    if 'price' not in errors:
        if v.tier == 'basic':
            v.price = Decimal('0')                      # basic videos are always free
        elif not (Decimal('1') <= (v.price or 0) <= PRICE_MAX):
            errors['price'] = f'{TIERS[v.tier]} videos need a price from SAR 1 to SAR {PRICE_MAX:.0f}.'
