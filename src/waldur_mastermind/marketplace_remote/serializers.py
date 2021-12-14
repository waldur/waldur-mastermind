from rest_framework import serializers

from . import models


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

    class Meta:
        model = models.ProjectUpdateRequest

        fields = (
            'uuid',
            'state',
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
        )
