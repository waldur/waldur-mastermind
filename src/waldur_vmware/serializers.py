from __future__ import unicode_literals

from rest_framework import serializers
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers

from . import constants, models


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        structure_serializers.BaseServiceSerializer):

    SERVICE_ACCOUNT_FIELDS = {
        'backend_url': _('VMware auth URL'),
        'username': _('VMware user username'),
        'password': _('VMware user password'),
    }

    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'default_cluster_id': _('ID of VMware cluster that will be used for virtual machines provisioning'),
        'max_cpu': _('Maximum vCPU for each VM'),
        'max_ram': _('Maximum RAM for each VM, MB'),
        'max_disk': _('Maximum capacity for each disk, MB'),
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.VMwareService
        required_fields = ('backend_url', 'username', 'password', 'default_cluster_id')


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


class NestedNetworkSerializer(core_serializers.AugmentedSerializerMixin,
                              core_serializers.HyperlinkedRelatedModelSerializer):
    class Meta(object):
        model = models.Network
        fields = ('uuid', 'url', 'name', 'type')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
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

    cluster = serializers.HyperlinkedRelatedField(
        view_name='vmware-cluster-detail',
        lookup_field='uuid',
        queryset=models.Cluster.objects.all(),
        allow_null=True,
        required=False,
    )

    datastore = serializers.HyperlinkedRelatedField(
        view_name='vmware-datastore-detail',
        lookup_field='uuid',
        queryset=models.Datastore.objects.all(),
        allow_null=True,
        required=False,
    )

    networks = NestedNetworkSerializer(queryset=models.Network.objects.all(), many=True, required=False)

    def get_guest_os_name(self, vm):
        return constants.GUEST_OS_CHOICES.get(vm.guest_os)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.VirtualMachine
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'guest_os', 'guest_os_name', 'cores', 'cores_per_socket', 'ram', 'disk', 'disks',
            'runtime_state', 'template', 'cluster', 'networks', 'datastore',
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'guest_os', 'template', 'cluster', 'networks', 'datastore',
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

    def _validate_attributes(self, attrs):
        template_attrs = {'cores', 'cores_per_socket', 'ram', 'disk', 'guest_os'}
        template = attrs.get('template')
        missing_attributes = template_attrs - set(attrs.keys())
        if template:
            if attrs.get('guest_os'):
                raise serializers.ValidationError(
                    'It is not possible to customize guest OS when template is used.')
            for attr in template_attrs:
                old_value = attrs.get(attr)
                if not old_value:
                    attrs[attr] = getattr(template, attr)
        elif missing_attributes:
            attr_list = ', '.join(missing_attributes)
            raise serializers.ValidationError(
                'These fields are required when template is not used: %s.' % attr_list)
        return attrs

    def _validate_cpu(self, attrs, options):
        actual_cpu = attrs.get('cores')
        max_cpu = options.get('max_cpu')
        if actual_cpu and max_cpu and actual_cpu > max_cpu:
            raise serializers.ValidationError('Requested amount of CPU exceeds offering limit.')

    def _validate_ram(self, attrs, options):
        actual_ram = attrs.get('ram')
        max_ram = options.get('max_ram')
        if actual_ram and max_ram and actual_ram > max_ram:
            raise serializers.ValidationError('Requested amount of RAM exceeds offering limit.')

    def _validate_disk(self, attrs, options):
        template = attrs.get('template')
        max_disk = options.get('max_disk')
        actual_disk = template.disk if template else 0
        if actual_disk and max_disk and actual_disk > max_disk:
            raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

    def _validate_limits(self, attrs):
        if self.instance:
            spl = self.instance.service_project_link
        else:
            spl = attrs['service_project_link']

        options = spl.service.settings.options
        self._validate_cpu(attrs, options)
        self._validate_ram(attrs, options)
        self._validate_disk(attrs, options)

    def _validate_cluster(self, attrs):
        cluster = attrs.get('cluster')
        spl = attrs['service_project_link']

        if cluster:
            if cluster.settings != spl.service.settings:
                raise serializers.ValidationError('This cluster is not available for this service.')

            if not cluster.customercluster_set.filter(customer=spl.project.customer).exists():
                raise serializers.ValidationError('This cluster is not available for this customer.')
        else:
            default_cluster_id = spl.service.settings.options.get('default_cluster_id')
            try:
                attrs['cluster'] = models.Cluster.objects.filter(
                    settings=spl.service.settings,
                    backend_id=default_cluster_id).get()
            except models.Cluster.DoesNotExist:
                raise serializers.ValidationError('Default cluster is not defined for this service.')
        return attrs

    def _validate_datastore(self, attrs):
        datastore = attrs.get('datastore')
        spl = attrs['service_project_link']

        if datastore:
            if datastore.settings != spl.service.settings:
                raise serializers.ValidationError('This datastore is not available for this service.')

            if not datastore.customerdatastore_set.filter(customer=spl.project.customer).exists():
                raise serializers.ValidationError('This datastore is not available for this customer.')

    def _validate_networks(self, attrs):
        networks = attrs.get('networks', [])
        spl = attrs['service_project_link']

        for network in networks:
            if network.settings != spl.service.settings:
                raise serializers.ValidationError('This network is not available for this service.')

            if not network.customernetwork_set.filter(customer=spl.project.customer).exists():
                raise serializers.ValidationError('This network is not available for this customer.')

    def validate(self, attrs):
        attrs = super(VirtualMachineSerializer, self).validate(attrs)

        if self.instance:
            self._validate_limits(attrs)
        else:
            attrs = self._validate_attributes(attrs)
            attrs = self._validate_cluster(attrs)

            self._validate_limits(attrs)
            self._validate_datastore(attrs)
            self._validate_networks(attrs)

        return attrs

    def create(self, validated_data):
        networks = validated_data.pop('networks', [])
        vm = super(VirtualMachineSerializer, self).create(validated_data)
        vm.networks.add(*networks)
        return vm


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

    def _validate_size(self, vm, attrs):
        options = vm.service_project_link.service.settings.options

        actual_disk = attrs.get('size')
        max_disk = options.get('max_disk')
        if actual_disk and max_disk and actual_disk > max_disk:
            raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        vm = self.context['view'].get_object()
        self._validate_size(vm, attrs)
        attrs['vm'] = vm
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

        options = self.instance.service_project_link.service.settings.options

        max_disk = options.get('max_disk')
        if max_disk and value > max_disk:
            raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

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


class ClusterSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Cluster
        fields = (
            'url', 'uuid', 'name',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class NetworkSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Network
        fields = (
            'url', 'uuid', 'name', 'type',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class DatastoreSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Datastore
        fields = (
            'url', 'uuid', 'name', 'type', 'capacity', 'free_space'
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
