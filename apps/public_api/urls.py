from django.urls import path
from . import live, views

app_name = 'public_api'

urlpatterns = [
    # Public read
    path('plans/',      views.public_plans,      name='plans'),
    path('courses/',    views.public_courses,    name='courses'),
    path('facilities/', views.public_facilities, name='facilities'),
    path('trainers/',   views.public_trainers,   name='trainers'),
    path('referral/',   views.public_referral,   name='referral'),
    path('articles/',   views.public_articles,   name='articles'),
    path('live/',       live.public_live,        name='live'),

    # Public register
    path('photo-check/',                    views.public_photo_check,      name='photo-check'),
    path('liveness/start/',                 views.public_liveness_start,   name='liveness-start'),
    path('liveness/frame/',                 views.public_liveness_frame,   name='liveness-frame'),
    path('register/',                       views.public_register,         name='register'),
    path('register/<int:reg_id>/status/',   views.public_register_status,  name='register-status'),
    path('register/<int:reg_id>/payment/attach/', views.public_payment_attach, name='register-payment-attach'),

    # Moyasar
    path('payments/moyasar/verify/',  views.public_payment_verify, name='moyasar-verify'),
    path('payments/moyasar/webhook/', views.moyasar_webhook,       name='moyasar-webhook'),
]