from rest_framework import serializers
from .models import (
    TrainerProfile, TrainerAvailability,
    Course, CourseModule, Enrollment,
    TrainerSession,
    CourseReview, TrainerReview,
)


# ============================================
# TRAINERS
# ============================================
class TrainerUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    full_name = serializers.CharField()
    avatar = serializers.ImageField()


class TrainerProfileSerializer(serializers.ModelSerializer):
    user = TrainerUserSerializer(read_only=True)
    total_courses = serializers.SerializerMethodField()
    availability = serializers.SerializerMethodField()

    class Meta:
        model = TrainerProfile
        fields = [
            'id', 'user', 'title', 'title_ar', 'credentials', 'credentials_ar', 'bio', 'bio_ar', 'photo_url',
            'is_verified', 'is_accepting_clients',
            'specialties', 'specialties_ar', 'rating', 'total_reviews', 'total_students',
            'years_experience', 'hourly_rate',
            'offers_1on1', 'offers_consultation',
            'total_courses', 'availability',
        ]

    def get_total_courses(self, obj):
        return obj.courses.filter(status='published').count()

    def get_availability(self, obj):
        slots = obj.availability.filter(is_active=True)
        return TrainerAvailabilitySerializer(slots, many=True).data


class TrainerAvailabilitySerializer(serializers.ModelSerializer):
    day_label = serializers.CharField(source='get_day_of_week_display', read_only=True)

    class Meta:
        model = TrainerAvailability
        fields = ['id', 'day_of_week', 'day_label', 'start_time', 'end_time', 'is_active']


# ============================================
# COURSES
# ============================================
class CourseModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseModule
        fields = [
            'id', 'order', 'title', 'title_ar', 'description', 'description_ar',
            'week_number', 'day_number',
            'video_url', 'pdf_url', 'duration_minutes',
            'is_locked_until_prev',
        ]


class CourseListSerializer(serializers.ModelSerializer):
    free_for_me = serializers.SerializerMethodField()   # member can enrol without paying

    def get_free_for_me(self, obj):
        from apps.payments.gateway import course_is_free_for
        request = self.context.get('request')
        if request is None or not request.user.is_authenticated:
            return not obj.price or obj.price <= 0
        return course_is_free_for(request.user, obj)

    trainer_name = serializers.CharField(source='trainer.user.full_name', read_only=True)
    trainer_title = serializers.CharField(source='trainer.title', read_only=True)
    trainer_title_ar = serializers.CharField(source='trainer.title_ar', read_only=True)
    trainer_avatar = serializers.ImageField(source='trainer.user.avatar', read_only=True)

    class Meta:
        model = Course
        fields = [
            'id', 'title', 'title_ar', 'subtitle', 'subtitle_ar', 'category', 'level',
            'cover_image_url', 'duration_weeks', 'days_per_week',
            'estimated_minutes_per_session',
            'price', 'is_included_with_pro', 'free_for_me',
            'rating', 'total_reviews', 'total_enrolled',
            'is_featured',
            'trainer_name', 'trainer_title', 'trainer_title_ar', 'trainer_avatar',
            'created_at',
        ]


class CourseDetailSerializer(serializers.ModelSerializer):
    free_for_me = serializers.SerializerMethodField()   # member can enrol without paying

    def get_free_for_me(self, obj):
        from apps.payments.gateway import course_is_free_for
        request = self.context.get('request')
        if request is None or not request.user.is_authenticated:
            return not obj.price or obj.price <= 0
        return course_is_free_for(request.user, obj)

    trainer = TrainerProfileSerializer(read_only=True)
    modules = CourseModuleSerializer(many=True, read_only=True)

    class Meta:
        model = Course
        fields = [
            'id', 'title', 'title_ar', 'subtitle', 'subtitle_ar', 'description', 'description_ar', 'category', 'level',
            'cover_image_url', 'promo_video_url',
            'duration_weeks', 'days_per_week', 'estimated_minutes_per_session',
            'price', 'is_included_with_pro', 'free_for_me',
            'status', 'is_featured',
            'rating', 'total_reviews', 'total_enrolled',
            'trainer', 'modules',
            'created_at', 'updated_at',
        ]


# ============================================
# ENROLLMENTS
# ============================================
class EnrollmentSerializer(serializers.ModelSerializer):
    course = CourseListSerializer(read_only=True)
    course_id = serializers.IntegerField(write_only=True)
    # Worked out from the days actually done, so it stays right when lessons are added or removed.
    progress_percent = serializers.SerializerMethodField()
    completed_count = serializers.SerializerMethodField()
    total_modules = serializers.SerializerMethodField()

    def get_completed_count(self, obj):
        return obj.completed_modules.count()

    def get_total_modules(self, obj):
        return obj.course.modules.count()

    def get_progress_percent(self, obj):
        total = self.get_total_modules(obj)
        return int(self.get_completed_count(obj) * 100 / total) if total else 0

    class Meta:
        model = Enrollment
        fields = [
            'id', 'course', 'course_id', 'status',
            'enrolled_at', 'completed_at',
            'progress_percent', 'completed_count', 'total_modules', 'amount_paid', 'payment_ref',
        ]
        read_only_fields = [
            'id', 'status', 'enrolled_at', 'completed_at',
            'progress_percent', 'amount_paid', 'payment_ref',
        ]


# ============================================
# SESSIONS
# ============================================
class TrainerSessionSerializer(serializers.ModelSerializer):
    trainer_name = serializers.CharField(source='trainer.user.full_name', read_only=True)
    trainer_title = serializers.CharField(source='trainer.title', read_only=True)
    trainer_title_ar = serializers.CharField(source='trainer.title_ar', read_only=True)
    trainer_photo = serializers.CharField(source='trainer.photo_url', read_only=True)
    trainer_id = serializers.IntegerField(write_only=True, required=False)

    class Meta:
        model = TrainerSession
        fields = [
            'id', 'trainer', 'trainer_name', 'trainer_title', 'trainer_title_ar', 'trainer_photo', 'trainer_id',
            'scheduled_at', 'duration_minutes', 'status',
            'focus_area', 'notes', 'amount_charged',
            'created_at',
        ]
        read_only_fields = ['id', 'trainer', 'status', 'duration_minutes', 'amount_charged', 'created_at']


# ============================================
# REVIEWS
# ============================================
class CourseReviewSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='member.full_name', read_only=True)

    class Meta:
        model = CourseReview
        fields = ['id', 'member_name', 'rating', 'comment', 'created_at']
        read_only_fields = ['id', 'created_at']


class TrainerReviewSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source='member.full_name', read_only=True)

    class Meta:
        model = TrainerReview
        fields = ['id', 'member_name', 'rating', 'comment', 'created_at']
        read_only_fields = ['id', 'created_at']