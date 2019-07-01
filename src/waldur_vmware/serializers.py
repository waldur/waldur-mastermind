from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
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


class NestedDiskSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.Disk
        fields = ('url', 'uuid', 'size')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'vmware-disk-detail'},
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

    guest_os = serializers.ChoiceField(
        choices=constants.GUEST_OS_CHOICES.items(),
        required=False,
        allow_null=True,
    )

    guest_os_name = serializers.SerializerMethodField()

    disks = NestedDiskSerializer(many=True, read_only=True)

    template = serializers.HyperlinkedRelatedField(
        view_name='vmware-template-detail',
        lookup_field='uuid',
        queryset=models.Template.objects.all(),
        allow_null=True,
        required=False,
        write_only=True,
    )

    def get_guest_os_name(self, vm):
        return constants.GUEST_OS_CHOICES.get(vm.guest_os)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.VirtualMachine
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'guest_os', 'guest_os_name', 'cores', 'cores_per_socket', 'ram', 'disk', 'disks',
            'runtime_state', 'template'
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'guest_os',
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'disk', 'runtime_state',
        )
        extra_kwargs = dict(
            cores={'required': False},
            cores_per_socket={'required': False},
            ram={'required': False},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def create(self, validated_data):
        template_attrs = {'cores', 'cores_per_socket', 'ram', 'guest_os'}
        template = validated_data.get('template')
        missing_attributes = template_attrs - set(validated_data.keys())
        if template:
            if validated_data.get('guest_os'):
                raise serializers.ValidationError(
                    'It is not possible to customize guest OS when template is used.')
            for attr in template_attrs:
                old_value = validated_data.get(attr)
                if not old_value:
                    validated_data[attr] = getattr(template, attr)
        elif missing_attributes:
            attr_list = ', '.join(missing_attributes)
            raise serializers.ValidationError(
                'These fields are required when template is not used: %s.' % attr_list)
        return super(VirtualMachineSerializer, self).create(validated_data)


class DiskSerializer(structure_serializers.BaseResourceSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='vmware-detail',
        read_only=True,
        lookup_field='uuid',
    )

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='vmware-spl-detail',
        read_only=True,
    )

    service_settings = serializers.HyperlinkedRelatedField(
        view_name='servicesettings-detail',
        lookup_field='uuid',
        read_only=True,
    )

    project = serializers.HyperlinkedRelatedField(
        view_name='project-detail',
        lookup_field='uuid',
        read_only=True,
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Disk
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'size', 'vm'
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'size',
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'vm',
        )
        extra_kwargs = dict(
            vm={
                'view_name': 'vmware-virtual-machine-detail',
                'lookup_field': 'uuid',
            },
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs['vm'] = vm = self.context['view'].get_object()
        attrs['service_project_link'] = vm.service_project_link
        return super(DiskSerializer, self).validate(attrs)


class DiskExtendSerializer(serializers.ModelSerializer):
    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Disk
        fields = ('size',)

    def validate_size(self, value):
        if value <= self.instance.size:
            raise serializers.ValidationError(
                _('Disk size should be greater than %s') % self.instance.size)
        return value


class TemplateSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Template
        fields = (
            'url', 'uuid', 'name', 'description', 'created', 'modified',
            'guest_os', 'guest_os_name', 'cores', 'cores_per_socket', 'ram',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    guest_os_name = serializers.SerializerMethodField()

    def get_guest_os_name(self, template):
        return constants.GUEST_OS_CHOICES.get(template.guest_os)
