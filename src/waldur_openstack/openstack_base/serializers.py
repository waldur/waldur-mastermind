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
