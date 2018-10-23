from __future__ import unicode_literals

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_core.core.serializers import GenericRelatedField
from waldur_core.structure.models import Customer, Project


class DailyHistoryQuotaSerializer(serializers.Serializer):
    scope = GenericRelatedField(related_models=(Project, Customer))
    quota_names = serializers.ListField(child=serializers.CharField(), required=False)
    start = serializers.DateField(format='%Y-%m-%d', required=False)
    end = serializers.DateField(format='%Y-%m-%d', required=False)

    def validate(self, attrs):
        if 'quota_names' not in attrs:
            attrs['quota_names'] = attrs['scope'].get_quotas_names
        if 'end' not in attrs:
            attrs['end'] = timezone.now().date()
        if 'start' not in attrs:
            attrs['start'] = timezone.now().date() - timedelta(days=30)
        if attrs['start'] >= attrs['end']:
            raise serializers.ValidationError(
                _('Invalid period specified. `start` should be lesser than `end`.')
            )
        return attrs
