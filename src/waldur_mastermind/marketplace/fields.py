from django.urls import NoReverseMatch
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace.models import Plan


class PublicPlanField(serializers.HyperlinkedRelatedField):
    def get_url(self, obj, view_name, request, format):
        try:
            return super().get_url(obj, view_name, request, format)
        except NoReverseMatch:
            return self.context['request'].build_absolute_uri(
                reverse(
                    'marketplace-public-offering-plan-detail',
                    kwargs={'uuid': obj.offering.uuid.hex, 'plan_uuid': obj.uuid.hex},
                )
            )

    def get_attribute(self, instance):
        if isinstance(instance, Plan):
            return instance
        return super().get_attribute(instance)
