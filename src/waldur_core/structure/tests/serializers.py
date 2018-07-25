from rest_framework import serializers

from waldur_core.structure import serializers as structure_serializers
from . import models


class ServiceSerializer(structure_serializers.BaseServiceSerializer):
    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'tenant_name': '',
        'availability_zone': '',
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.TestService
        required_fields = 'backend_url', 'username', 'password'


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.TestServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'test-detail'},
        }


class NewInstanceSerializer(structure_serializers.VirtualMachineSerializer):

    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='test-detail',
        read_only=True,
        lookup_field='uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='test-spl-detail',
        queryset=models.TestServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.TestNewInstance
