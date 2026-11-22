"""
GET  /api/admin/approvals/?status=pending&type=&submitted_by=&q=&page=   the admin's approval queue
GET  /api/admin/approvals/counts/                                       waiting per kind
GET  /api/admin/approvals/<id>/                                         one request, with the live values
POST /api/admin/approvals/<id>/approve/ {payload?, note?}               publish / apply (admin's edits in payload)
POST /api/admin/approvals/<id>/reject/ {note}
GET  /api/admin/staff-activity/?actor=&role=&area=&q=&from=&to=&page=   who did what
GET  /api/approvals/mine/?status=                                       what I sent and what happened to it
POST /api/approvals/mine/<id>/withdraw/                                 take back a waiting request
"""
from datetime import datetime, time

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.roles import STAFF_ROLES, IsAdminRole

from . import registry
from .models import ChangeRequest, StaffActivity

PAGE = 25


def _page(request, qs):
    try:
        page = max(1, int(request.query_params.get('page') or 1))
    except ValueError:
        page = 1
    total = qs.count()
    return page, total, max(1, -(-total // PAGE)), qs[(page - 1) * PAGE: page * PAGE]


def _person(u):
    if u is None:
        return None
    return {'id': u.id, 'name': u.full_name or u.email, 'email': u.email, 'role': u.role}


def _row(cr, obj=None, full=False):
    k = registry.kind_of(cr)
    obj = obj if obj is not None else registry.target(cr)
    current = k.snapshot(obj) if (k and obj is not None) else {}
    current = registry._clean(current)
    if cr.kind == 'update':
        changes = [{'field': f, 'before': cr.before.get(f, current.get(f)), 'after': v} for f, v in cr.payload.items()]
    else:
        changes = [{'field': f, 'before': None, 'after': v} for f, v in (current or cr.before).items()]
    row = {
        'id': cr.id, 'type': cr.target_type, 'type_label': k.label if k else cr.target_type,
        'type_label_ar': k.label_ar if k else cr.target_type, 'kind': cr.kind, 'kind_label': cr.get_kind_display(),
        'status': cr.status, 'summary': cr.summary, 'object_id': cr.object_id, 'exists': obj is not None,
        'submitted_by': _person(cr.submitted_by), 'reviewed_by': _person(cr.reviewed_by),
        'review_note': cr.review_note, 'created_at': cr.created_at, 'updated_at': cr.updated_at,
        'reviewed_at': cr.reviewed_at, 'changes': changes,
        'link': k.path('admin', obj) if (k and obj is not None) else '',
    }
    if full:
        row.update({'current': current, 'payload': cr.payload, 'before': cr.before,
                    'editable': list(k.editable) if (k and cr.kind != 'delete' and cr.status == 'pending') else []})
    return row


@api_view(['GET'])
@permission_classes([IsAdminRole])
def queue(request):
    qs = ChangeRequest.objects.select_related('submitted_by', 'reviewed_by')
    st = request.query_params.get('status', 'pending')
    if st in dict(ChangeRequest.STATUS_CHOICES):
        qs = qs.filter(status=st)
    if request.query_params.get('type'):
        qs = qs.filter(target_type=request.query_params['type'])
    if request.query_params.get('kind') in ('create', 'update', 'delete'):
        qs = qs.filter(kind=request.query_params['kind'])
    if request.query_params.get('submitted_by'):
        qs = qs.filter(submitted_by_id=request.query_params['submitted_by'])
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(summary__icontains=q) | Q(submitted_by__email__icontains=q)
                       | Q(submitted_by__first_name__icontains=q) | Q(submitted_by__last_name__icontains=q))
    qs = qs.order_by('created_at' if st == 'pending' else '-reviewed_at', 'id')
    page, total, pages, rows = _page(request, qs)
    return Response({'count': total, 'page': page, 'pages': pages, 'results': [_row(cr) for cr in rows],
                     'types': [{'value': k, 'label': v.label, 'label_ar': v.label_ar} for k, v in registry.KINDS.items()],
                     # New courses keep their own review (admin → Coaches → Courses); counted here so nothing is missed.
                     'courses_waiting': _courses_waiting()})


@api_view(['GET'])
@permission_classes([IsAdminRole])
def counts(request):
    by = dict(ChangeRequest.objects.filter(status='pending').values_list('target_type').annotate(n=Count('id')))
    return Response({'total': sum(by.values()), 'by_type': by})


@api_view(['GET'])
@permission_classes([IsAdminRole])
def detail(request, cr_id):
    return Response(_row(get_object_or_404(ChangeRequest, id=cr_id), full=True))


@api_view(['POST'])
@permission_classes([IsAdminRole])
def approve(request, cr_id):
    cr = get_object_or_404(ChangeRequest, id=cr_id)
    payload = request.data.get('payload')
    if payload is not None and not isinstance(payload, dict):
        return Response({'payload': 'Send the edited values as an object.'}, status=400)
    try:
        registry.approve(cr, request.user, payload=payload, note=str(request.data.get('note') or ''))
    except registry.ReviewError as e:
        return Response(e.errors, status=400)
    return Response(_row(cr, full=True))


@api_view(['POST'])
@permission_classes([IsAdminRole])
def reject(request, cr_id):
    cr = get_object_or_404(ChangeRequest, id=cr_id)
    note = str(request.data.get('note') or '').strip()
    if not note:
        return Response({'note': 'Say why, so the person can fix it.'}, status=400)
    try:
        registry.reject(cr, request.user, note=note)
    except registry.ReviewError as e:
        return Response(e.errors, status=400)
    return Response(_row(cr, full=True))


def _day(value, end=False):
    try:
        d = datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(datetime.combine(d, time.max if end else time.min))


@api_view(['GET'])
@permission_classes([IsAdminRole])
def staff_activity(request):
    qs = StaffActivity.objects.select_related('actor')
    p = request.query_params
    if p.get('actor'):
        qs = qs.filter(actor_id=p['actor'])
    if p.get('role') in STAFF_ROLES:
        qs = qs.filter(role=p['role'])
    if p.get('area'):
        qs = qs.filter(area=p['area'])
    if p.get('from') and _day(p['from']):
        qs = qs.filter(created_at__gte=_day(p['from']))
    if p.get('to') and _day(p['to'], end=True):
        qs = qs.filter(created_at__lte=_day(p['to'], end=True))
    q = (p.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(action__icontains=q) | Q(path__icontains=q) | Q(actor__email__icontains=q)
                       | Q(actor__first_name__icontains=q) | Q(actor__last_name__icontains=q))
    page, total, pages, rows = _page(request, qs)
    people = (StaffActivity.objects.values('actor_id', 'actor__first_name', 'actor__last_name', 'actor__email', 'role')
              .annotate(n=Count('id')).order_by('actor__first_name'))
    return Response({
        'count': total, 'page': page, 'pages': pages,
        'results': [{'id': a.id, 'actor': _person(a.actor), 'role': a.role, 'action': a.action, 'area': a.area,
                     'method': a.method, 'path': a.path, 'status_code': a.status_code, 'created_at': a.created_at}
                    for a in rows],
        'people': [{'id': x['actor_id'], 'name': f"{x['actor__first_name']} {x['actor__last_name']}".strip()
                    or x['actor__email'], 'role': x['role'], 'actions': x['n']} for x in people if x['actor_id']],
        'areas': sorted(set(StaffActivity.objects.values_list('area', flat=True))),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mine(request):
    if request.user.role not in STAFF_ROLES:
        return Response({'count': 0, 'page': 1, 'pages': 1, 'results': []})
    qs = ChangeRequest.objects.filter(submitted_by=request.user).select_related('reviewed_by')
    if request.query_params.get('status') in dict(ChangeRequest.STATUS_CHOICES):
        qs = qs.filter(status=request.query_params['status'])
    if request.query_params.get('type'):
        qs = qs.filter(target_type=request.query_params['type'])
    page, total, pages, rows = _page(request, qs)
    return Response({'count': total, 'page': page, 'pages': pages, 'results': [_row(cr) for cr in rows]})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def withdraw(request, cr_id):
    cr = get_object_or_404(ChangeRequest, id=cr_id, submitted_by=request.user)
    if cr.status != 'pending':
        return Response({'detail': 'Only a waiting request can be taken back.'}, status=400)
    if cr.kind == 'create':
        return Response({'detail': 'Delete the new item instead.'}, status=400)
    cr.status = 'withdrawn'
    cr.save(update_fields=['status', 'updated_at'])
    return Response(_row(cr))


def _courses_waiting():
    """Courses waiting for review that aren't in the queue (sent before courses joined it)."""
    from apps.catalog.models import Course
    queued = ChangeRequest.objects.filter(target_type='coach.course_new', status='pending').values('object_id')
    return Course.objects.filter(status='pending').exclude(id__in=queued).count()
