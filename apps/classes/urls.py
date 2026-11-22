from django.urls import path
from . import views

app_name = 'classes'

urlpatterns = [
    path('', views.member_classes, name='member-classes'),
    path('<int:class_id>/book/', views.book_class, name='book'),
    path('<int:class_id>/cancel/', views.cancel_class, name='cancel'),
    path('<int:class_id>/posts/', views.member_class_posts, name='posts'),
    path('schedule/', views.classes_schedule, name='schedule'),
    path('stats/', views.classes_stats, name='stats'),
    path('admin/', views.admin_classes, name='admin-list'),
    path('admin/<int:class_id>/', views.admin_class_detail, name='admin-detail'),
    path('admin/<int:class_id>/roster/', views.admin_class_roster, name='admin-roster'),
    path('admin/<int:class_id>/roster/<int:enrollment_id>/remove/', views.admin_roster_remove, name='admin-roster-remove'),
]
