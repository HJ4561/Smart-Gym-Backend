from django.db import models
from django.conf import settings


class Exercise(models.Model):
    """Catalog of exercises linked to machines."""
    CATEGORY_CHOICES = [
        ('strength', 'Strength'),
        ('cardio', 'Cardio'),
        ('functional', 'Functional'),
        ('combat', 'Combat'),
        ('recovery', 'Recovery'),
    ]

    MUSCLE_CHOICES = [
        ('chest', 'Chest'),
        ('back', 'Back'),
        ('shoulders', 'Shoulders'),
        ('arms', 'Arms'),
        ('legs', 'Legs'),
        ('core', 'Core'),
        ('full_body', 'Full Body'),
    ]

    # NEW: equipment & coaching metadata
    equipment_type = models.CharField(
        max_length=60,
        blank=True,
        default='',
        help_text='e.g. Iso-Lateral Chest Press, Cable Bay, Barbell'
    )
    setup_instructions = models.TextField(blank=True)
    setup_instructions_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English
    default_rest_seconds = models.IntegerField(default=90)
    recommended_weight_kg = models.DecimalField(
        max_digits=6, decimal_places=2,
        null=True, blank=True
    )
    technique_phases = models.JSONField(
        default=list, blank=True,
        help_text='[{"num":"Phase 01","title":"...","desc":"..."}]'
    )
    target_intensity = models.CharField(max_length=60, blank=True, default='RPE 8.0')
    cadence = models.CharField(max_length=60, blank=True, default='3-0-1-0')


    name = models.CharField(max_length=100, unique=True)
    name_ar = models.CharField(max_length=100, blank=True, db_default='')   # Arabic; empty = show the English
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    primary_muscle = models.CharField(max_length=20, choices=MUSCLE_CHOICES)
    secondary_muscles = models.JSONField(default=list, blank=True)

    # Machine link
    machine_id = models.CharField(max_length=20, blank=True, unique=False)
    qr_tag_id = models.CharField(max_length=30, blank=True)

    # Instructions
    description = models.TextField(blank=True)
    description_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English
    technique_cues = models.JSONField(default=list, blank=True)
    video_url = models.URLField(blank=True)
    image_url = models.URLField(blank=True)

    # Baseline
    is_compound = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'exercises'
        ordering = ['category', 'name']

    def __str__(self):
        return self.name


class ExerciseVideo(models.Model):
    """
    A how-to video for a machine, added by a coach (or the gym). Basic videos are free for every
    member; Advanced and Premium ones are included in some membership plans
    (MembershipPlan.video_access) or bought one by one (VideoPurchase).
    """
    TIER_CHOICES = [
        ('basic', 'Basic (free)'),
        ('advanced', 'Advanced'),
        ('premium', 'Premium'),
    ]
    TIER_RANK = {'basic': 0, 'advanced': 1, 'premium': 2}

    exercise = models.ForeignKey(Exercise, on_delete=models.CASCADE, related_name='videos')
    coach = models.ForeignKey('catalog.TrainerProfile', on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='machine_videos')
    tier = models.CharField(max_length=10, choices=TIER_CHOICES, default='basic')
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    video_url = models.URLField()
    duration_minutes = models.PositiveIntegerField(default=5)
    price = models.DecimalField(max_digits=8, decimal_places=2, default=0)   # 0 for basic videos
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'exercise_videos'
        ordering = ['exercise_id', 'created_at']

    def __str__(self):
        return f'{self.exercise.name} — {self.title} ({self.tier})'


class VideoPurchase(models.Model):
    """A member bought one Advanced/Premium video; it stays unlocked for them."""
    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='video_purchases')
    video = models.ForeignKey(ExerciseVideo, on_delete=models.PROTECT, related_name='purchases')
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'video_purchases'
        unique_together = ['member', 'video']


class Workout(models.Model):
    """A single workout session."""
    STATUS_CHOICES = [
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('skipped', 'Skipped'),
    ]

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='workouts',
    )
    name = models.CharField(max_length=120, default='Training Session')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_progress')

    # Timing
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.IntegerField(null=True, blank=True)

    # Totals (calculated)
    total_exercises = models.IntegerField(default=0)
    total_sets = models.IntegerField(default=0)
    total_reps = models.IntegerField(default=0)
    total_tonnage_kg = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    avg_velocity = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    calories_burned = models.IntegerField(null=True, blank=True)

    # Metadata
    notes = models.TextField(blank=True)
    rpe_avg = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)

    class Meta:
        db_table = 'workouts'
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.member.email} — {self.name} @ {self.started_at:%Y-%m-%d}"


class WorkoutExercise(models.Model):
    """An exercise within a workout."""
    workout = models.ForeignKey(
        Workout,
        on_delete=models.CASCADE,
        related_name='exercises',
    )
    exercise = models.ForeignKey(
        Exercise,
        on_delete=models.PROTECT,
        related_name='workout_entries',
    )
    order = models.IntegerField(default=0)
    notes = models.TextField(blank=True)
    rest_seconds = models.IntegerField(default=90)

    class Meta:
        db_table = 'workout_exercises'
        ordering = ['order']

    def __str__(self):
        return f"{self.workout} — {self.exercise.name}"


class WorkoutSet(models.Model):
    """A single set within a workout exercise."""
    workout_exercise = models.ForeignKey(
        WorkoutExercise,
        on_delete=models.CASCADE,
        related_name='sets',
    )
    set_number = models.IntegerField()
    reps = models.IntegerField()
    weight_kg = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    # VBT / Velocity
    mean_velocity = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True)
    peak_velocity = models.DecimalField(max_digits=5, decimal_places=3, null=True, blank=True)

    # Subjective
    rpe = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    is_warmup = models.BooleanField(default=False)
    is_pr = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'workout_sets'
        ordering = ['set_number']

    def __str__(self):
        return f"Set {self.set_number}: {self.reps} x {self.weight_kg}kg"


class PersonalRecord(models.Model):
    """A member's best performance on an exercise."""
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='personal_records',
    )
    exercise = models.ForeignKey(
        Exercise,
        on_delete=models.CASCADE,
        related_name='personal_records',
    )
    best_weight_kg = models.DecimalField(max_digits=8, decimal_places=2)
    best_reps = models.IntegerField()
    best_estimated_1rm = models.DecimalField(max_digits=8, decimal_places=2)

    achieved_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'personal_records'
        unique_together = ['member', 'exercise']
        ordering = ['-achieved_at']

    def __str__(self):
        return f"{self.member.email} — {self.exercise.name}: {self.best_weight_kg}kg x {self.best_reps}"


class WorkoutTemplate(models.Model):
    """Pre-built workout templates (courses, programs)."""
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=50, blank=True)  # Hypertrophy, Power, etc.
    difficulty = models.CharField(max_length=20, default='intermediate')  # beginner/intermediate/advanced

    estimated_duration_minutes = models.IntegerField(default=60)

    is_public = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'workout_templates'
        ordering = ['name']

    def __str__(self):
        return self.name


class TemplateExercise(models.Model):
    """Exercise entry within a workout template."""
    template = models.ForeignKey(
        WorkoutTemplate,
        on_delete=models.CASCADE,
        related_name='exercises',
    )
    exercise = models.ForeignKey(Exercise, on_delete=models.PROTECT)
    order = models.IntegerField(default=0)
    target_sets = models.IntegerField(default=3)
    target_reps = models.CharField(max_length=20, default='8-12')
    target_weight_note = models.CharField(max_length=100, blank=True)
    rest_seconds = models.IntegerField(default=90)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'template_exercises'
        ordering = ['order']

    def __str__(self):
        return f"{self.template.name} — {self.exercise.name}"