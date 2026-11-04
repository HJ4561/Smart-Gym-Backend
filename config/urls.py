from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from apps.site_content.urls import public_urlpatterns as site_content_public
from apps.zatca.urls import member_urlpatterns as zatca_member_urls


urlpatterns = [
    path('django-admin/', admin.site.urls),

    # ----- Existing APIs -----
    path('api/', include('apps.core.urls')),
    path('api/auth/', include('apps.accounts.urls')),
    path('api/members/', include('apps.members.urls')),
    path('api/workouts/', include('apps.workouts.urls')),
    path('api/catalog/', include('apps.catalog.urls')),
    path('api/payments/', include('apps.payments.urls')),
    path('api/nutrition/', include('apps.nutrition.urls')),
    path('api/shop/', include('apps.shop.urls')),
    path('api/notifications/', include('apps.notifications.urls')),
    path('api/facilities/', include('apps.facilities.urls')),
    path('api/classes/', include('apps.classes.urls')),
    path('api/staff/', include('apps.staff.urls')),
    path('api/reports/', include('apps.reports.urls')),
    path('api/cms/', include('apps.cms.urls')),
    path('api/attendance/', include('apps.attendance.urls')),
    path('api/dashboard/', include('apps.dashboard.urls')),

    # ----- Existing admin dashboard API -----
    # (members, machines, finance, analytics, audit-feed, arena-heatmap, etc.)
    path('api/admin/', include('apps.admin_api.urls')),
    path('api/coach/', include('apps.coach.urls')),

    # ----- Public site (no auth) -----
    path('api/public/', include('apps.public_api.urls')),
    path('api/public/', include((site_content_public, 'site_content_public'))),

    # ----- Public-site content editor + contact inbox (admin/manager) -----
    path('api/admin/site-content/', include('apps.site_content.urls')),

    # ----- Registration approval + gym-entry lookup -----
    # Mounted under a distinct prefix to avoid colliding with apps.admin_api
    path('api/admin/registrations-api/', include('apps.public_api.admin_urls')),

    # ----- ZATCA e-invoicing: settings, go-live wizard, invoice log, retry cron -----
    path('api/admin/zatca/', include('apps.zatca.urls')),
    path('api/zatca/', include((zatca_member_urls, 'zatca_member'))),
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)