from django.db import models
from django.conf import settings


# ============================================
# NUTRITIONIST PROFILES
# ============================================
class NutritionistProfile(models.Model):
    """Extended profile for users with role=nutritionist."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='nutritionist_profile',
    )
    title = models.CharField(max_length=120)  # "Head of Metabolic Architecture"
    title_ar = models.CharField(max_length=120, blank=True, db_default='')   # Arabic; empty = show the English
    credentials = models.CharField(max_length=200, blank=True)  # "RD, CSSD"
    credentials_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    bio = models.TextField(blank=True)
    bio_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English
    photo_url = models.URLField(blank=True)

    is_verified = models.BooleanField(default=False)
    is_accepting_clients = models.BooleanField(default=True)

    # Specialties (list of strings)
    specialties = models.JSONField(default=list, blank=True)
    specialties_ar = models.JSONField(default=list, blank=True, null=True)   # Arabic; empty = show the English

    # Metrics
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    total_reviews = models.IntegerField(default=0)
    total_clients = models.IntegerField(default=0)
    years_experience = models.IntegerField(default=0)

    # Pricing
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, default=120)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'nutritionist_profiles'
        ordering = ['-rating']

    def __str__(self):
        return f"{self.user.full_name} — {self.title}"


# ============================================
# NUTRITION PLANS
# ============================================
class NutritionPlan(models.Model):
    """A personalized nutrition plan assigned by a nutritionist."""
    STATUS_CHOICES = [
        ('pending_payment', 'Awaiting payment'),   # requested, consultation fee not paid yet
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('completed', 'Completed'),
        ('archived', 'Archived'),
    ]
    GOAL_CHOICES = [
        ('hypertrophy', 'Hypertrophy / Bulking'),
        ('fat_loss', 'Rapid Fat Loss / Cut'),
        ('recomp', 'Body Recomposition'),
        ('maintenance', 'Maintenance'),
        ('endurance', 'Endurance Performance'),
    ]

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='nutrition_plans',
    )
    nutritionist = models.ForeignKey(
        NutritionistProfile,
        on_delete=models.PROTECT,
        related_name='plans',
    )

    name = models.CharField(max_length=120, default='Personalized Nutrition Plan')
    goal = models.CharField(max_length=20, choices=GOAL_CHOICES, default='hypertrophy')

    # Daily targets
    target_calories = models.IntegerField(default=2500)
    target_protein_g = models.IntegerField(default=180)
    target_carbs_g = models.IntegerField(default=300)
    target_fats_g = models.IntegerField(default=80)
    target_water_ml = models.IntegerField(default=3500)

    # Cycle
    cycle_weeks = models.IntegerField(default=8)
    started_at = models.DateField(auto_now_add=True)
    ends_at = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    notes = models.TextField(blank=True)
    # Consultation fee: the nutritionist's rate when the plan was requested; paid before it starts.
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Set when staff review the starter targets a member requested (null = still the template).
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'nutrition_plans'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.member.email} — {self.name} ({self.goal})"


# ============================================
# MEAL LOGS
# ============================================
class MealLog(models.Model):
    """A single meal entry."""
    MEAL_CHOICES = [
        ('breakfast', 'Breakfast'),
        ('lunch', 'Lunch'),
        ('dinner', 'Dinner'),
        ('snack', 'Snack'),
        ('pre_workout', 'Pre-Workout'),
        ('post_workout', 'Post-Workout'),
    ]

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='meal_logs',
    )
    plan = models.ForeignKey(
        NutritionPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='meals',
    )

    meal_type = models.CharField(max_length=20, choices=MEAL_CHOICES)
    description = models.CharField(max_length=200)
    photo_url = models.URLField(blank=True)

    # Macros
    calories = models.IntegerField(default=0)
    protein_g = models.DecimalField(max_digits=6, decimal_places=1, default=0)
    carbs_g = models.DecimalField(max_digits=6, decimal_places=1, default=0)
    fats_g = models.DecimalField(max_digits=6, decimal_places=1, default=0)

    logged_at = models.DateTimeField(auto_now_add=True)
    logged_date = models.DateField(auto_now_add=True)

    class Meta:
        db_table = 'meal_logs'
        ordering = ['-logged_at']

    def __str__(self):
        return f"{self.member.email} — {self.get_meal_type_display()}: {self.description}"


# ============================================
# WATER TRACKING
# ============================================
class WaterIntake(models.Model):
    """Daily water intake log."""
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='water_logs',
    )
    date = models.DateField(auto_now_add=True)
    amount_ml = models.IntegerField(default=0)

    class Meta:
        db_table = 'water_intake'
        unique_together = ['member', 'date']
        ordering = ['-date']

    def __str__(self):
        return f"{self.member.email} — {self.date}: {self.amount_ml}ml"


# ============================================
# DEXA SCANS
# ============================================
class DexaScan(models.Model):
    """Body composition scan."""
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='dexa_scans',
    )
    scan_date = models.DateField(auto_now_add=True)

    # Composition
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2)
    body_fat_percent = models.DecimalField(max_digits=5, decimal_places=2)
    lean_mass_kg = models.DecimalField(max_digits=6, decimal_places=2)
    fat_mass_kg = models.DecimalField(max_digits=6, decimal_places=2)
    visceral_fat_kg = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    # Notes
    notes = models.TextField(blank=True)
    report_url = models.URLField(blank=True)

    class Meta:
        db_table = 'dexa_scans'
        ordering = ['-scan_date']

    def __str__(self):
        return f"{self.member.email} — {self.body_fat_percent}% BF ({self.scan_date})"