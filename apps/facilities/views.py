# apps/facilities/views.py — REPLACE COMPLETELY (kept compatible with your model)

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from datetime import timedelta

from .models import Facility, FacilityBooking
from apps.admin_api.permissions import IsAdminUser


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminUser])
def facilities_list(request):
    """
    GET /api/facilities/
    Query: ?category=&search=
    """
    qs = Facility.objects.filter(is_active=True)

    category = request.query_params.get('category')
    if category and category != 'all':
        qs = qs.filter(category=category)

    search = request.query_params.get('search')
    if search:
        qs = qs.filter(name__icontains=search)

    cutoff = timezone.now() - timedelta(hours=24)

    data = []
    for f in qs:
        bookings_24h = f.bookings.filter(start_time__gte=cutoff).count()

        data.append({
            'id': f.id,
            'code': f.code,
            'name': f.name,
            'name_ar': f.name_ar,
            'description_ar': f.description_ar,
            'category': f.category,
            'zone': f.zone,
            'capacity': f.capacity,
            'current': f.current_occupancy,
            'utilization': f.utilization,
            'status': f.status,
            'temp': f'{f.temp_celsius}°C',
            'amenities': f.amenities or [],
            'bookings24h': bookings_24h,
            'revenue24h': 0,
        })

    return Response(data)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminUser])
def facilities_stats(request):
    """
    GET /api/facilities/stats/
    """
    active_qs = Facility.objects.filter(is_active=True)
    total = active_qs.count()

    peak = active_qs.order_by('-current_occupancy').first()

    active_bookings = FacilityBooking.objects.filter(
        status='booked',
        start_time__gte=timezone.now(),
    ).count()

    return Response({
        'total': total,
        'peak_utilization': peak.utilization if peak else 0,
        'peak_name': peak.name if peak else '—',
        'active_bookings': active_bookings,
        'avg_dwell': 42,
    })