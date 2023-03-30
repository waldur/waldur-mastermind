from rest_framework import serializers

from waldur_core.core import signals as core_signals
from waldur_mastermind.marketplace import serializers as marketplace_serializers

from . import PLUGIN_NAME, constants, models


class CredentialsSerializer(serializers.Serializer):
    api_url = serializers.URLField()
    token = serializers.CharField()


class OfferingCreateSerializer(CredentialsSerializer):
    remote_offering_uuid = serializers.CharField()
    local_category_uuid = serializers.CharField()
    local_customer_uuid = serializers.CharField()
    remote_customer_uuid = serializers.CharField()


class ProjectUpdateRequestSerializer(serializers.ModelSerializer):
    state = serializers.ReadOnlyField(source='get_state_display')
    customer_name = serializers.ReadOnlyField(source='project.customer.name')
    offering_name = serializers.ReadOnlyField(source='offering.name')
    offering_uuid = serializers.ReadOnlyField(source='offering.uuid')

    reviewed_by_full_name = serializers.ReadOnlyField(source='reviewed_by.full_name')
    reviewed_by_uuid = serializers.ReadOnlyField(source='reviewed_by.uuid')

    class Meta:
        model = models.ProjectUpdateRequest

        fields = (
            'uuid',
            'state',
            'customer_name',
            'offering_name',
            'offering_uuid',
            'created',
            'reviewed_at',
            'reviewed_by_full_name',
            'reviewed_by_uuid',
            'review_comment',
            'old_name',
            'new_name',
            'old_description',
            'new_description',
            'old_end_date',
            'new_end_date',
            'old_oecd_fos_2007_code',
            'new_oecd_fos_2007_code',
            'old_is_industry',
            'new_is_industry',
            'created_by',
        )


def mark_synced_fields_as_read_only(sender, fields, serializer, **kwargs):
    if serializer.instance and serializer.instance.type == PLUGIN_NAME:
        for field_name in constants.OFFERING_FIELDS:
            fields[field_name] = serializers.ReadOnlyField()


core_signals.pre_serializer_fields.connect(
    mark_synced_fields_as_read_only,
    sender=marketplace_serializers.OfferingUpdateSerializer,
)
