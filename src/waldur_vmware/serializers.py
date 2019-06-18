from __future__ import unicode_literals

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers

from . import models


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        structure_serializers.BaseServiceSerializer):

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.VMwareService


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.VMwareServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'vmware-detail'},
        }
