from rest_framework import serializers
from .models import Milestone, MemberMilestone


class MilestoneSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    current_days = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()
    reward_code = serializers.SerializerMethodField()   # only once claimed

    class Meta:
        model = Milestone
        fields = [
            'id', 'tier', 'tier_ar', 'title', 'title_ar', 'description', 'description_ar', 'icon',
            'tier_type', 'target_days', 'reward_value', 'reward_value_ar', 'reward_code',
            'status', 'progress', 'current_days', 'days_left',
        ]

    def _get_member_milestone(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return None
        cache = self.context.setdefault('_mm', {})
        if obj.id not in cache:
            cache[obj.id] = MemberMilestone.objects.filter(milestone=obj, member=request.user).first()
        return cache[obj.id]

    def get_reward_code(self, obj):
        mm = self._get_member_milestone(obj)
        return obj.reward_code if mm and mm.status == 'claimed' else None

    def get_status(self, obj):
        mm = self._get_member_milestone(obj)
        return mm.status if mm else 'locked'

    def get_progress(self, obj):
        mm = self._get_member_milestone(obj)
        return mm.progress if mm else 0

    def get_current_days(self, obj):
        mm = self._get_member_milestone(obj)
        return mm.current_days if mm else 0

    def get_days_left(self, obj):
        mm = self._get_member_milestone(obj)
        if not mm:
            return obj.target_days
        return max(0, obj.target_days - mm.current_days)