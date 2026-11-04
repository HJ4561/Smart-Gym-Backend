from rest_framework import serializers
from .models import (
    NutritionistProfile, NutritionPlan,
    MealLog, WaterIntake, DexaScan,
)


# ============================================
# NUTRITIONIST USER (nested)
# ============================================
class NutritionistUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    full_name = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()

    def get_full_name(self, obj):
        # Works with any User model — tries full_name attr, falls back to first+last, then email
        if hasattr(obj, 'full_name') and obj.full_name:
            return obj.full_name
        first = getattr(obj, 'first_name', '') or ''
        last = getattr(obj, 'last_name', '') or ''
        name = f"{first} {last}".strip()
        return name or obj.email

    def get_avatar(self, obj):
        # Avatar may be a URLField, ImageField, or missing entirely
        avatar = getattr(obj, 'avatar', None)
        if not avatar:
            return None
        # ImageField returns FieldFile — call .url
        if hasattr(avatar, 'url'):
            try:
                return avatar.url
            except Exception:
                return None
        return str(avatar)


class NutritionistProfileSerializer(serializers.ModelSerializer):
    user = NutritionistUserSerializer(read_only=True)
    is_featured = serializers.SerializerMethodField()

    class Meta:
        model = NutritionistProfile
        fields = [
            'id', 'user', 'title', 'title_ar', 'credentials', 'credentials_ar', 'bio', 'bio_ar', 'photo_url',
            'is_verified', 'is_accepting_clients', 'specialties', 'specialties_ar',
            'rating', 'total_reviews', 'total_clients',
            'years_experience', 'hourly_rate', 'is_featured',
        ]

    def get_is_featured(self, obj):
        # Top verified nutritionist with rating >= 4.5 is featured
        return bool(obj.is_verified and float(obj.rating or 0) >= 4.5)


# ============================================
# NUTRITION PLAN
# ============================================
class NutritionPlanSerializer(serializers.ModelSerializer):
    nutritionist_name = serializers.SerializerMethodField()
    nutritionist_credentials = serializers.CharField(
        source='nutritionist.credentials', read_only=True
    )
    nutritionist_avatar = serializers.SerializerMethodField()
    nutritionist_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = NutritionPlan
        fields = [
            'id', 'name', 'goal', 'status', 'price',
            'target_calories', 'target_protein_g', 'target_carbs_g', 'target_fats_g',
            'target_water_ml',
            'cycle_weeks', 'started_at', 'ends_at',
            'notes', 'reviewed_at',
            'nutritionist_id', 'nutritionist_name', 'nutritionist_credentials', 'nutritionist_avatar',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'started_at', 'reviewed_at']

    def get_nutritionist_name(self, obj):
        u = obj.nutritionist.user
        if hasattr(u, 'full_name') and u.full_name:
            return u.full_name
        name = f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}".strip()
        return name or u.email

    def get_nutritionist_avatar(self, obj):
        u = obj.nutritionist.user
        avatar = getattr(u, 'avatar', None)
        if not avatar:
            return None
        if hasattr(avatar, 'url'):
            try:
                return avatar.url
            except Exception:
                return None
        return str(avatar)


# ============================================
# MEAL LOG
# ============================================
class MealLogSerializer(serializers.ModelSerializer):
    meal_label = serializers.CharField(source='get_meal_type_display', read_only=True)

    class Meta:
        model = MealLog
        fields = [
            'id', 'meal_type', 'meal_label', 'description', 'photo_url',
            'calories', 'protein_g', 'carbs_g', 'fats_g',
            'logged_at', 'logged_date',
        ]
        read_only_fields = ['id', 'logged_at', 'logged_date']


# ============================================
# WATER
# ============================================
class WaterIntakeSerializer(serializers.ModelSerializer):
    class Meta:
        model = WaterIntake
        fields = ['id', 'date', 'amount_ml']


# ============================================
# DEXA
# ============================================
class DexaScanSerializer(serializers.ModelSerializer):
    class Meta:
        model = DexaScan
        fields = [
            'id', 'scan_date', 'weight_kg', 'body_fat_percent',
            'lean_mass_kg', 'fat_mass_kg', 'visceral_fat_kg',
            'notes', 'report_url',
        ]
        read_only_fields = ['id', 'scan_date']