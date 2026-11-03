from django.urls import path
from . import views

app_name = 'nutrition'

urlpatterns = [
    # Directory
    path('nutritionists/', views.nutritionists_list, name='nutritionists-list'),
    path('nutritionists/<int:nutritionist_id>/', views.nutritionist_detail, name='nutritionist-detail'),

    # Plan
    path('plan/', views.my_plan, name='my-plan'),
    path('plans/', views.my_plans, name='my-plans'),
    path('plan/request/', views.request_plan, name='request-plan'),

    # Meals
    path('meals/', views.meals, name='meals'),
    path('meals/<int:meal_id>/', views.delete_meal, name='delete-meal'),

    # Water
    path('water/', views.water, name='water'),

    # Summary
    path('summary/', views.daily_summary, name='daily-summary'),

    # DEXA
    path('dexa/', views.dexa_history, name='dexa-history'),
    path('dexa/latest/', views.latest_dexa, name='latest-dexa'),
]