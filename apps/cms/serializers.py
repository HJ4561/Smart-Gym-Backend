# apps/cms/serializers.py — REPLACE COMPLETELY

from rest_framework import serializers
from .models import Article


class ArticleSerializer(serializers.ModelSerializer):
    author_display = serializers.SerializerMethodField()
    status_label = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Article
        fields = [
            'id', 'article_id', 'title', 'title_ar', 'slug', 'excerpt', 'excerpt_ar', 'content', 'content_ar',
            'category', 'tags', 'cover_image_url',
            'author', 'author_display', 'author_name',
            'status', 'status_label', 'is_featured',
            'read_time_minutes', 'views_count',
            'published_at', 'scheduled_for',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'slug']

    def get_author_display(self, obj):
        if obj.author:
            u = obj.author
            if hasattr(u, 'full_name') and u.full_name:
                return u.full_name
            name = f"{getattr(u, 'first_name', '')} {getattr(u, 'last_name', '')}".strip()
            return name or u.email
        return obj.author_name or 'Anonymous'