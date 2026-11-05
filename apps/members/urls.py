from django.urls import path
from . import views

app_name = 'members'

urlpatterns = [
    path('plans/', views.plans_list, name='plans-list'),
    path('membership/', views.my_membership, name='my-membership'),
    path('check-ins/', views.check_ins, name='check-ins'),
    path('check-ins/<int:check_in_id>/check-out/', views.check_out, name='check-out'),
    path('pass/', views.my_pass, name='my-pass'),
    path('referral/', views.my_referral, name='my-referral'),
    path('facilities/', views.my_facilities, name='my-facilities'),   # signed QR token; staff resolve it in Member Lookup
]