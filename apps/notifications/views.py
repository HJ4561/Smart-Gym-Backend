from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone

from .models import Notification, NotificationPreference
from .serializers import NotificationSerializer, NotificationPreferenceSerializer


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_notifications(request):
    """
    GET /api/notifications/?category=&unread=true&q=&from=&to=
    """
    from apps.core import listing
    qs = Notification.objects.filter(recipient=request.user)
    qs = listing.search(qs, request.query_params.get('q'), ['title', 'message'])
    qs = listing.date_range(qs, request.query_params, 'created_at')

    category = request.query_params.get('category')
    if category:
        qs = qs.filter(category=category)

    if request.query_params.get('unread') == 'true':
        qs = qs.filter(is_read=False)

    return Response(NotificationSerializer(qs[:100], many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def unread_count(request):
    count = Notification.objects.filter(recipient=request.user, is_read=False).count()
    return Response({'unread_count': count})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_read(request, notification_id):
    try:
        n = Notification.objects.get(id=notification_id, recipient=request.user)
    except Notification.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    n.is_read = True
    n.read_at = timezone.now()
    n.save()
    return Response(NotificationSerializer(n).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_all_read(request):
    Notification.objects.filter(recipient=request.user, is_read=False).update(
        is_read=True, read_at=timezone.now()
    )
    return Response({'detail': 'All marked read.'})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_notification(request, notification_id):
    try:
        n = Notification.objects.get(id=notification_id, recipient=request.user)
    except Notification.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    n.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def my_preferences(request):
    pref, _ = NotificationPreference.objects.get_or_create(user=request.user)

    if request.method == 'GET':
        return Response(NotificationPreferenceSerializer(pref).data)

    serializer = NotificationPreferenceSerializer(pref, data=request.data, partial=True)
    if serializer.is_valid():
        serializer.save()
        return Response(serializer.data)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)