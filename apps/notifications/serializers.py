from rest_framework import serializers

from apps.core.i18n import language

from .models import Notification, NotificationPreference


class NotificationSerializer(serializers.ModelSerializer):
    """`title` and `message` in the reader's language (English when there is no Arabic)."""

    class Meta:
        model = Notification
        fields = [
            'id', 'category', 'priority', 'icon', 'title', 'message',
            'action_label', 'action_path', 'is_read', 'read_at', 'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'read_at']

    def to_representation(self, obj):
        row = super().to_representation(obj)
        if language() == 'ar':
            row['title'] = obj.title_ar or obj.title
            row['message'] = obj.message_ar or obj.message
        return row


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = [
            'push_enabled', 'email_enabled', 'whatsapp_enabled',
            'sms_enabled', 'marketing_enabled',
        ]