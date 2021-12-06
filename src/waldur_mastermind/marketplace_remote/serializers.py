from rest_framework import serializers

from waldur_core.core.serializers import AugmentedSerializerMixin
from waldur_core.structure.serializers import ProjectDetailsSerializerMixin

from . import models


class CredentialsSerializer(serializers.Serializer):
    api_url = serializers.URLField()
    token = serializers.CharField()


class OfferingCreateSerializer(CredentialsSerializer):
    remote_offering_uuid = serializers.CharField()
    local_category_uuid = serializers.CharField()
    local_customer_uuid = serializers.CharField()
    remote_customer_uuid = serializers.CharField()


class ProjectUpdateRequestSerializer(
    ProjectDetailsSerializerMixin,
    serializers.HyperlinkedModelSerializer,
    AugmentedSerializerMixin,
):
    state = serializers.ReadOnlyField(source='get_state_display')

    old_name = serializers.ReadOnlyField(source='project.name')
    new_name = serializers.ReadOnlyField(source='name')

    old_description = serializers.ReadOnlyField(source='project.description')
    new_description = serializers.ReadOnlyField(source='description')

    old_type = serializers.ReadOnlyField(source='project.type.name')
    new_type = serializers.ReadOnlyField(source='type.name')

    old_oecd_fos_2007_code = serializers.ReadOnlyField(
        source='project.oecd_fos_2007_code'
    )
    new_oecd_fos_2007_code = serializers.ReadOnlyField(source='oecd_fos_2007_code')

    class Meta:
        model = models.ProjectUpdateRequest
        related_paths = {
            'offering': ('uuid', 'name'),
            'reviewed_by': ('full_name', 'uuid'),
        }

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
            'old_type_name',
            'new_type_name',
            'old_oecd_fos_2007_code',
            'new_oecd_fos_2007_code',
        )
