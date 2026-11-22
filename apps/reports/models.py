from django.db import models
from django.conf import settings
from apps.core.sides import SideManager, side_field


class ReportTemplate(models.Model):
    """A report template that can be generated on-demand."""
    TEMPLATE_CHOICES = [
        ('revenue', 'Revenue Report'),
        ('members', 'Member Report'),
        ('attendance', 'Attendance Report'),
        ('machines', 'Machine Utilization'),
        ('trainers', 'Trainer Performance'),
        ('finance', 'Financial Statement'),
    ]
    FREQUENCY_CHOICES = [
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('quarterly', 'Quarterly'),
    ]

    key = models.CharField(max_length=40, choices=TEMPLATE_CHOICES, unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=40, default='assessment')
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default='monthly')
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'report_templates'
        ordering = ['-is_featured', 'key']

    def __str__(self):
        return self.title


class GeneratedReport(models.Model):
    """A generated report record (files can be stored on S3)."""
    FORMAT_CHOICES = [
        ('PDF', 'PDF'),
        ('CSV', 'CSV'),
        ('XLSX', 'Excel'),
    ]

    template = models.ForeignKey(
        ReportTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generated',
    )
    report_id = models.CharField(max_length=30, unique=True)  # RPT-0182
    name = models.CharField(max_length=200)
    report_type = models.CharField(max_length=40, default='revenue')
    period = models.CharField(max_length=60, blank=True)
    format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default='PDF')
    size_kb = models.IntegerField(default=0)

    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='generated_reports',
    )

    file_url = models.URLField(blank=True)
    # The CSV itself, frozen when generated (downloads always match what was generated).
    content = models.TextField(blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    row_count = models.IntegerField(default=0)
    generated_at = models.DateTimeField(auto_now_add=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'generated_reports'
        ordering = ['-generated_at']

    def __str__(self):
        return f"{self.report_id} — {self.name}"