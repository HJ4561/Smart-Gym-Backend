from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = 'accounts'

urlpatterns = [
    # Auth
    path('register/', views.register, name='register'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('logout/', views.logout, name='logout'),

    # User
    path('me/', views.me, name='me'),
    path('change-password/', views.change_password, name='change-password'),
    path('password/forgot/', views.forgot_password, name='forgot-password'),
    path('password/reset/', views.reset_password, name='reset-password'),

    # 2-step verification
    path('2fa/', views.two_factor_status, name='2fa-status'),
    path('2fa/verify/', views.two_factor_login_verify, name='2fa-verify'),
    path('2fa/resend/', views.two_factor_resend, name='2fa-resend'),
    path('2fa/setup/method/', views.two_factor_setup_method, name='2fa-setup-method'),
    path('2fa/setup/confirm/', views.two_factor_setup_confirm, name='2fa-setup-confirm'),
    path('2fa/enable/', views.two_factor_enable, name='2fa-enable'),
    path('2fa/send-code/', views.two_factor_send_code, name='2fa-send-code'),
    path('2fa/disable/', views.two_factor_disable, name='2fa-disable'),
    path('2fa/backup-codes/', views.two_factor_backup_codes, name='2fa-backup-codes'),
    path('2fa/devices/forget/', views.two_factor_forget_devices, name='2fa-forget-devices'),
]