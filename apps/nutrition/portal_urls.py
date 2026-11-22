"""The nutritionist portal: /api/nutritionist/…  (see apps.nutrition.portal)."""
from django.urls import path

from . import portal

app_name = 'nutritionist_portal'

urlpatterns = [
    path('me/', portal.me, name='me'),
    path('me/photo/', portal.photo, name='photo'),
    path('plans/', portal.plans, name='plans'),
    path('plans/<int:plan_id>/', portal.plan_detail, name='plan'),
    path('clients/', portal.clients, name='clients'),
    path('clients/<int:member_id>/', portal.client, name='client'),
    path('clients/<int:member_id>/meals/', portal.client_meals, name='client-meals'),
    path('clients/<int:member_id>/scans/', portal.client_scans, name='client-scans'),
]
