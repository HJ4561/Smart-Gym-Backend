"""
Admin → Reviews: every member review of a course or a coach (this gym side only), with filters, and
delete. Deleting a review takes it off the page and out of the rating at once; a review still waiting in
the approval queue is withdrawn from it.

  GET    /api/admin/reviews/?type=course|trainer&status=approved|pending|rejected&rating=1-5&q=&page=
  DELETE /api/admin/reviews/<course|trainer>/<id>/
"""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.core.roles import IsAdminRole

from .models import CourseReview, TrainerReview
from .reviews import refresh_course_rating, refresh_trainer_rating

PAGE = 25
KINDS = {'course': (CourseReview, 'catalog.course_review'), 'trainer': (TrainerReview, 'catalog.trainer_review')}


def _member(u):
    return {'id': u.id, 'name': u.full_name or u.email, 'email': u.email}


def _row(kind, r):
    about = r.course.title if kind == 'course' else (r.trainer.user.full_name or r.trainer.user.email)
    return {'id': r.id, 'type': kind, 'about': about,
            'about_id': r.course_id if kind == 'course' else r.trainer_id,
            'member': _member(r.member), 'rating': r.rating, 'comment': r.comment, 'status': r.status,
            'created_at': r.created_at}


def _filtered(kind, params):
    model, _ = KINDS[kind]
    qs = model.objects.select_related('member', 'course' if kind == 'course' else 'trainer__user')
    if params.get('status') in ('approved', 'pending', 'rejected'):
        qs = qs.filter(status=params['status'])
    if str(params.get('rating') or '') in ('1', '2', '3', '4', '5'):
        qs = qs.filter(rating=int(params['rating']))
    q = (params.get('q') or '').strip()
    if q:
        about = Q(course__title__icontains=q) if kind == 'course' else (
            Q(trainer__user__first_name__icontains=q) | Q(trainer__user__last_name__icontains=q))
        qs = qs.filter(about | Q(comment__icontains=q) | Q(member__email__icontains=q)
                       | Q(member__first_name__icontains=q) | Q(member__last_name__icontains=q))
    return qs


@api_view(['GET'])
@permission_classes([IsAdminRole])
def reviews(request):
    p = request.query_params
    kinds = [p['type']] if p.get('type') in KINDS else list(KINDS)
    rows = [_row(k, r) for k in kinds for r in _filtered(k, p)]
    rows.sort(key=lambda r: r['created_at'], reverse=True)
    try:
        page = max(int(p.get('page', 1)), 1)
    except ValueError:
        page = 1
    pages = max((len(rows) + PAGE - 1) // PAGE, 1)
    page = min(page, pages)
    counts = {k: KINDS[k][0].objects.count() for k in KINDS}
    return Response({'count': len(rows), 'page': page, 'pages': pages, 'counts': counts,
                     'results': rows[(page - 1) * PAGE: page * PAGE]})


@api_view(['DELETE'])
@permission_classes([IsAdminRole])
def review_detail(request, kind, review_id):
    if kind not in KINDS:
        return Response({'detail': 'Not found.'}, status=404)
    model, key = KINDS[kind]
    r = get_object_or_404(model, id=review_id)
    target = r.course if kind == 'course' else r.trainer
    from apps.approvals.registry import pending_for
    pending_for(key, r.pk).update(status='withdrawn', review_note='The review was deleted.')
    r.delete()
    (refresh_course_rating if kind == 'course' else refresh_trainer_rating)(target)
    return Response(status=204)
