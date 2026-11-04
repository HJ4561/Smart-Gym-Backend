from django.db import models
from django.conf import settings
from django.utils.text import slugify


class Article(models.Model):
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('published', 'Published'),
        ('archived', 'Archived'),
    ]

    article_id = models.CharField(max_length=20, unique=True)  # ART-001
    title = models.CharField(max_length=200)
    title_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    excerpt = models.CharField(max_length=300, blank=True)
    excerpt_ar = models.CharField(max_length=300, blank=True, db_default='')   # Arabic; empty = show the English
    content = models.TextField(blank=True)
    content_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English

    category = models.CharField(max_length=60, default='Training')
    tags = models.JSONField(default=list, blank=True)

    cover_image_url = models.URLField(blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='articles',
    )
    author_name = models.CharField(max_length=120, blank=True)  # snapshot

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    is_featured = models.BooleanField(default=False)

    read_time_minutes = models.IntegerField(default=5)
    views_count = models.IntegerField(default=0)

    published_at = models.DateTimeField(null=True, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'articles'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.article_id} — {self.title}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)[:220]
        super().save(*args, **kwargs)