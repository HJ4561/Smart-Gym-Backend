from rest_framework import serializers
from .models import (
    Exercise, Workout, WorkoutExercise, WorkoutSet,
    PersonalRecord, WorkoutTemplate, TemplateExercise,
)


# ============================================
# EXERCISE
# ============================================
class ExerciseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Exercise
        fields = [
            'id', 'name', 'name_ar', 'category', 'primary_muscle', 'secondary_muscles',
            'machine_id', 'qr_tag_id', 'description', 'description_ar', 'technique_cues',
            'video_url', 'image_url', 'is_compound', 'is_active',
            # NEW fields
            'equipment_type', 'setup_instructions', 'setup_instructions_ar',
            'default_rest_seconds', 'recommended_weight_kg',
            'technique_phases', 'target_intensity', 'cadence',
        ]


# ============================================
# WORKOUT SET
# ============================================
class WorkoutSetSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkoutSet
        fields = [
            'id', 'set_number', 'reps', 'weight_kg',
            'mean_velocity', 'peak_velocity', 'rpe',
            'is_warmup', 'is_pr', 'created_at',
        ]
        read_only_fields = ['id', 'is_pr', 'created_at']


# ============================================
# WORKOUT EXERCISE
# ============================================
class WorkoutExerciseSerializer(serializers.ModelSerializer):
    exercise = ExerciseSerializer(read_only=True)
    exercise_id = serializers.IntegerField(write_only=True)
    sets = WorkoutSetSerializer(many=True, read_only=True)

    class Meta:
        model = WorkoutExercise
        fields = [
            'id', 'exercise', 'exercise_id', 'order',
            'notes', 'rest_seconds', 'sets',
        ]
        read_only_fields = ['id']


# ============================================
# WORKOUT
# ============================================
class WorkoutSerializer(serializers.ModelSerializer):
    exercises = WorkoutExerciseSerializer(many=True, read_only=True)

    class Meta:
        model = Workout
        fields = [
            'id', 'name', 'status',
            'started_at', 'completed_at', 'duration_minutes',
            'total_exercises', 'total_sets', 'total_reps', 'total_tonnage_kg',
            'avg_velocity', 'calories_burned', 'notes', 'rpe_avg',
            'exercises',
        ]
        read_only_fields = [
            'id', 'started_at', 'completed_at', 'duration_minutes',
            'total_exercises', 'total_sets', 'total_reps',
            'total_tonnage_kg', 'avg_velocity',
        ]


class WorkoutListSerializer(serializers.ModelSerializer):
    """Lighter version for lists."""
    class Meta:
        model = Workout
        fields = [
            'id', 'name', 'status',
            'started_at', 'completed_at', 'duration_minutes',
            'total_exercises', 'total_sets', 'total_tonnage_kg',
        ]


# ============================================
# PERSONAL RECORD
# ============================================
class PersonalRecordSerializer(serializers.ModelSerializer):
    exercise_name = serializers.CharField(source='exercise.name', read_only=True)
    exercise_name_ar = serializers.CharField(source='exercise.name_ar', read_only=True)

    class Meta:
        model = PersonalRecord
        fields = [
            'id', 'exercise', 'exercise_name', 'exercise_name_ar',
            'best_weight_kg', 'best_reps', 'best_estimated_1rm',
            'achieved_at',
        ]
        read_only_fields = ['id', 'achieved_at']


# ============================================
# TEMPLATES
# ============================================
class TemplateExerciseSerializer(serializers.ModelSerializer):
    exercise = ExerciseSerializer(read_only=True)

    class Meta:
        model = TemplateExercise
        fields = [
            'id', 'exercise', 'order', 'target_sets', 'target_reps',
            'target_weight_note', 'rest_seconds', 'notes',
        ]


class WorkoutTemplateSerializer(serializers.ModelSerializer):
    exercises = TemplateExerciseSerializer(many=True, read_only=True)

    class Meta:
        model = WorkoutTemplate
        fields = [
            'id', 'name', 'name_ar', 'description', 'description_ar', 'category', 'difficulty',
            'estimated_duration_minutes', 'is_public', 'is_active',
            'exercises', 'created_at',
        ]