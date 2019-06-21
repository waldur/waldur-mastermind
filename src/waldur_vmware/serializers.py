from __future__ import unicode_literals

from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers

from . import constants, models


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


class VirtualMachineSerializer(structure_serializers.BaseResourceSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='vmware-detail',
        read_only=True,
        lookup_field='uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='vmware-spl-detail',
        queryset=models.VMwareServiceProjectLink.objects.all(),
        allow_null=True,
        required=False,
    )

    guest_os = serializers.ChoiceField(choices=constants.GUEST_OS_CHOICES.items())

    guest_os_name = serializers.SerializerMethodField()

    def get_guest_os_name(self, vm):
        return constants.GUEST_OS_CHOICES.get(vm.guest_os)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.VirtualMachine
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'guest_os', 'guest_os_name', 'cores', 'cores_per_socket', 'ram', 'disk'
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'guest_os',
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'disk',
        )
