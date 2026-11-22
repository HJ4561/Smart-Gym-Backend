from django.urls import path

from . import views

admin_urlpatterns = [
    path('approvals/', views.queue, name='approvals'),
    path('approvals/counts/', views.counts, name='approvals-counts'),
    path('approvals/<int:cr_id>/', views.detail, name='approvals-detail'),
    path('approvals/<int:cr_id>/approve/', views.approve, name='approvals-approve'),
    path('approvals/<int:cr_id>/reject/', views.reject, name='approvals-reject'),
    path('staff-activity/', views.staff_activity, name='staff-activity'),
]

staff_urlpatterns = [
    path('mine/', views.mine, name='approvals-mine'),
    path('mine/<int:cr_id>/withdraw/', views.withdraw, name='approvals-withdraw'),
]
