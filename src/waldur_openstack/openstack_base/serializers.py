from rest_framework import serializers

from waldur_core.structure import serializers as structure_serializers

from . import models


class BaseVolumeTypeSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.BaseVolumeType
        fields = ('url', 'uuid', 'name', 'description', 'settings')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'settings': {
                'lookup_field': 'uuid',
                'view_name': 'servicesettings-detail',
            },
        }


class BaseSecurityGroupRuleSerializer(serializers.ModelSerializer):
    remote_group_name = serializers.ReadOnlyField(source='remote_group.name')
    remote_group_uuid = serializers.ReadOnlyField(source='remote_group.uuid')

    class Meta:
        model = models.BaseSecurityGroupRule
        fields = (
            'ethertype',
            'direction',
            'protocol',
            'from_port',
            'to_port',
            'cidr',
            'description',
            'remote_group_name',
            'remote_group_uuid',
        )
