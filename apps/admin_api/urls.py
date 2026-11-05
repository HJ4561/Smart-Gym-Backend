# apps/admin_api/urls.py
from django.urls import path
from . import catalog_ops, console, finance, insights, machines_ops, members_ops, nutrition_ops, ops, shop_ops, staff_ops, system_ops, views

app_name = 'admin_api'

urlpatterns = [
    # Overview
    path('overview/', views.overview, name='overview'),
    path('missing-arabic/', views.missing_arabic, name='missing-arabic'),

    # Members
    path('members/', members_ops.members_list, name='members-list'),
    path('members/stats/', members_ops.member_stats, name='member-stats'),
    path('members/bulk-action/', members_ops.bulk_member_action, name='member-bulk-action'),
    path('members/<int:user_id>/', members_ops.member_detail, name='member-detail'),
    path('members/<int:user_id>/update/', members_ops.update_member, name='member-update'),
    path('members/<int:user_id>/action/', members_ops.member_action, name='member-action'),

    # Machines
    path('machines/', machines_ops.machines, name='machines-list'),
    path('machines/stats/', machines_ops.machine_stats, name='machine-stats'),
    path('machines/categories/', machines_ops.machine_categories, name='machine-categories'),
    path('machines/<int:exercise_id>/', machines_ops.machine_update, name='machine-update'),
    path('machines/<int:exercise_id>/regenerate-qr/', machines_ops.regenerate_qr, name='regenerate-qr'),
    path('machines/<int:exercise_id>/videos/', machines_ops.machine_videos, name='machine-videos'),
    path('machine-videos/<int:video_id>/', machines_ops.machine_video_detail, name='machine-video-detail'),

        # Trainers
    path('trainers/', catalog_ops.trainers, name='trainers-list'),
    path('trainers/stats/', catalog_ops.trainer_stats, name='trainer-stats'),
    path('trainers/<int:trainer_id>/', catalog_ops.trainer_update, name='trainer-update'),
    path('trainers/<int:trainer_id>/photo/', catalog_ops.trainer_photo, name='trainer-photo'),
    path('trainers/<int:trainer_id>/portal-access/', catalog_ops.trainer_portal_access, name='trainer-portal-access'),
    path('courses/', catalog_ops.courses, name='courses'),
    path('courses/stats/', catalog_ops.course_stats, name='course-stats'),
    path('courses/<int:course_id>/', catalog_ops.course_detail, name='course-detail'),
    path('courses/<int:course_id>/cover/', catalog_ops.course_cover, name='course-cover'),

        # Staff
    path('staff/', staff_ops.staff, name='staff-list'),
    path('staff/stats/', staff_ops.staff_stats, name='staff-stats'),
    path('staff/<int:user_id>/', staff_ops.staff_update, name='staff-update'),
    path('staff/<int:user_id>/send-sign-in/', staff_ops.staff_send_signin, name='staff-send-sign-in'),
    path('staff/<int:user_id>/reset-2fa/', staff_ops.staff_reset_2fa, name='staff-reset-2fa'),
    # Nutrition
    path('nutrition/nutritionists/', nutrition_ops.nutritionists, name='nutritionists'),
    path('nutrition/nutritionists/<int:nutritionist_id>/', nutrition_ops.nutritionist_update, name='nutritionist-update'),
    path('nutrition/nutritionists/<int:nutritionist_id>/photo/', nutrition_ops.nutritionist_photo, name='nutritionist-photo'),
    path('nutrition/plans/', nutrition_ops.plans, name='nutrition-plans'),
    path('nutrition/plans/<int:plan_id>/', nutrition_ops.plan_update, name='nutrition-plan'),
    path('nutrition/members/<int:member_id>/scans/', nutrition_ops.member_scans, name='member-scans'),
    path('nutrition/members/<int:member_id>/meals/', nutrition_ops.member_meals, name='member-meals'),

    # Shop catalogue
    path('shop/products/', shop_ops.products, name='shop-products'),
    path('shop/products/next-sku/', shop_ops.product_next_sku, name='shop-product-next-sku'),
    path('shop/products/<int:product_id>/', shop_ops.product_detail, name='shop-product'),
    path('shop/products/<int:product_id>/image/', shop_ops.product_image, name='shop-product-image'),
    path('shop/stats/', shop_ops.shop_stats, name='shop-stats'),
    path('shop/categories/', shop_ops.categories, name='shop-categories'),
    path('shop/categories/<int:category_id>/', shop_ops.category_detail, name='shop-category'),
    path('shop/promos/', shop_ops.promos, name='shop-promos'),
    path('shop/promos/<int:promo_id>/', shop_ops.promo_detail, name='shop-promo'),

    # System settings
    path('settings/', system_ops.system_status, name='settings-get'),
    path('settings/security/', system_ops.security_settings, name='settings-security'),
    # Finance
    path('finance/overview/', finance.finance_overview, name='finance-overview'),
    path('finance/transactions/', finance.transactions_list, name='transactions-list'),
    path('finance/transactions/export/', views.transactions_export, name='transactions-export'),
        # Finance extras
    path('finance/revenue-monthly/', finance.revenue_monthly, name='revenue-monthly'),
    path('finance/revenue-streams/', finance.revenue_streams_view, name='revenue-streams'),
    path('finance/top-members/', finance.top_members_view, name='top-members'),
    path('finance/transactions/<int:transaction_id>/', finance.transaction_detail, name='transaction-detail'),
    path('finance/transactions/<int:transaction_id>/email-receipt/', finance.transaction_email_receipt, name='transaction-email-receipt'),

    # Dashboard extras
    path('flow-and-revenue/', views.flow_and_revenue, name='flow-and-revenue'),
    path('arena-heatmap/', views.arena_heatmap, name='arena-heatmap'),
    path('audit-feed/', views.audit_feed, name='audit-feed'),
    path('facility-nodes/', views.facility_nodes, name='facility-nodes'),


    # Header: alerts + global search (console.py)
    path('alerts/', console.alerts, name='alerts'),
    path('activity/', console.activity, name='activity'),
    path('search/', console.search, name='search'),

    # Member purchases back office (ops.py)
    path('facility-pricing/', ops.facility_pricing, name='facility-pricing'),
    path('facility-pricing/<int:item_id>/', ops.facility_pricing_update, name='facility-pricing-update'),
    path('shop/orders/', ops.shop_orders, name='shop-orders'),
    path('shop/orders/<str:order_number>/', ops.shop_order_detail, name='shop-order-detail'),
    path('shop/orders/<str:order_number>/status/', ops.shop_order_status, name='shop-order-status'),
    path('shop/orders/<str:order_number>/refund/', ops.shop_order_refund, name='shop-order-refund'),
    path('payments/review/', ops.payments_review, name='payments-review'),
    path('payments/intents/<int:intent_id>/refund/', ops.payment_refund, name='payment-refund'),
    path('trainer-hours/', ops.trainer_hours, name='trainer-hours'),
    path('trainer-hours/<int:trainer_id>/', ops.trainer_hours_update, name='trainer-hours-update'),
    path('trainer-sessions/', ops.trainer_sessions, name='trainer-sessions'),
    path('trainer-sessions/<int:session_id>/status/', ops.trainer_session_status, name='trainer-session-status'),

    # Analytics
    path('analytics/kpis/', insights.kpis, name='analytics-kpis'),
    path('analytics/growth/', insights.growth, name='analytics-growth'),
    path('analytics/insights/', insights.insights, name='ai-insights'),
]