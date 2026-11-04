from django.urls import path

from . import views

app_name = 'zatca'

urlpatterns = [
    path('status/', views.status, name='status'),
    path('settings/', views.update_settings, name='settings'),
    path('onboard/start/', views.onboard_start, name='onboard-start'),
    path('onboard/<int:device_id>/otp/', views.onboard_otp, name='onboard-otp'),
    path('onboard/<int:device_id>/checks/', views.onboard_checks, name='onboard-checks'),
    path('onboard/<int:device_id>/production/', views.onboard_production, name='onboard-production'),
    path('onboard/<int:device_id>/activate/', views.onboard_activate, name='onboard-activate'),
    path('documents/', views.documents, name='documents'),
    path('documents/<int:doc_id>/', views.document, name='document'),
    path('documents/<int:doc_id>/xml/', views.document_xml, name='document-xml'),
    path('documents/<int:doc_id>/send/', views.document_send, name='document-send'),
    path('retry/', views.retry, name='retry'),
    path('events/', views.events, name='events'),
    path('cron/', views.cron, name='cron'),
    path('companies/', views.companies, name='companies'),
    path('companies/<int:company_id>/', views.company, name='company'),
]

# /api/zatca/ — for members
member_urlpatterns = [
    path('companies/', views.my_companies, name='my-companies'),
    path('companies/<int:company_id>/', views.my_company, name='my-company'),
]
