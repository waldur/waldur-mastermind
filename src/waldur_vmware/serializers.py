from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from rest_framework import serializers
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import serializers as structure_serializers
from waldur_vmware.utils import is_basic_mode

from . import constants, models


def get_int_or_none(options, key):
    value = options.get(key)
    if not value:
        return value

    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return

    return value


class OptionsSerializer(serializers.Serializer):
    default_cluster_label = serializers.CharField(required=False)
    max_cpu = core_serializers.UnicodeIntegerField(min_value=1, required=False)
    max_cores_per_socket = core_serializers.UnicodeIntegerField(min_value=1, required=False)
    max_ram = core_serializers.UnicodeIntegerField(min_value=1, required=False)
    max_disk = core_serializers.UnicodeIntegerField(min_value=1, required=False)
    max_disk_total = core_serializers.UnicodeIntegerField(min_value=1, required=False)


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        structure_serializers.BaseServiceSerializer):

    SERVICE_ACCOUNT_FIELDS = {
        'backend_url': _('VMware auth URL'),
        'username': _('VMware user username'),
        'password': _('VMware user password'),
    }

    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'default_cluster_label': _('Label of VMware cluster that will be used for virtual machines provisioning'),
        'max_cpu': _('Maximum vCPU for each VM'),
        'max_cores_per_socket': _('Maximum number of cores per socket for each VM'),
        'max_ram': _('Maximum RAM for each VM, MiB'),
        'max_disk': _('Maximum capacity for each disk, MiB'),
        'max_disk_total': _('Maximum total size of the disk space per VM, MiB'),
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.VMwareService
        required_fields = ('backend_url', 'username', 'password', 'default_cluster_label')
        options_serializer = OptionsSerializer


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):
    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.VMwareServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'vmware-detail'},
        }


class LimitSerializer(serializers.Serializer):
    def to_representation(self, service_settings):
        fields = (
            'max_cpu',
            'max_cores_per_socket',
            'max_ram',
            'max_disk',
            'max_disk_total',
        )
        result = dict()
        for field in fields:
            result[field] = get_int_or_none(service_settings.options, field)
        return result


class NestedPortSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Port
        fields = ('url', 'uuid', 'name', 'mac_address', 'network')
        read_only_fields = ('mac_address',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'vmware-port-detail'},
            'network': {'lookup_field': 'uuid', 'view_name': 'vmware-network-detail'},
        }


class NestedDiskSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Disk
        fields = ('url', 'uuid', 'size')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'vmware-disk-detail'},
        }


class NestedNetworkSerializer(core_serializers.AugmentedSerializerMixin,
                              core_serializers.HyperlinkedRelatedModelSerializer):
    class Meta:
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
        choices=list(constants.GUEST_OS_CHOICES.items()),
        required=False,
        allow_null=True,
    )

    guest_os_name = serializers.SerializerMethodField()

    disks = NestedDiskSerializer(many=True, read_only=True)

    ports = NestedPortSerializer(many=True, read_only=True, source='port_set')

    template = serializers.HyperlinkedRelatedField(
        view_name='vmware-template-detail',
        lookup_field='uuid',
        queryset=models.Template.objects.all(),
        allow_null=True,
        required=False,
        write_only=True,
    )

    template_name = serializers.ReadOnlyField(source='template.name')

    cluster = serializers.HyperlinkedRelatedField(
        view_name='vmware-cluster-detail',
        lookup_field='uuid',
        queryset=models.Cluster.objects.all(),
        allow_null=True,
        required=False,
    )

    cluster_name = serializers.ReadOnlyField(source='cluster.name')

    datastore = serializers.HyperlinkedRelatedField(
        view_name='vmware-datastore-detail',
        lookup_field='uuid',
        queryset=models.Datastore.objects.all(),
        allow_null=True,
        required=False,
    )

    datastore_name = serializers.ReadOnlyField(source='datastore.name')

    folder = serializers.HyperlinkedRelatedField(
        view_name='vmware-folder-detail',
        lookup_field='uuid',
        queryset=models.Folder.objects.all(),
        allow_null=True,
        required=False,
    )

    folder_name = serializers.ReadOnlyField(source='folder.name')

    networks = NestedNetworkSerializer(queryset=models.Network.objects.all(),
                                       many=True, required=False, write_only=True)

    runtime_state = serializers.SerializerMethodField()

    tools_state = serializers.ReadOnlyField(source='get_tools_state_display')

    def get_runtime_state(self, vm):
        return dict(models.VirtualMachine.RuntimeStates.CHOICES).get(vm.runtime_state)

    def get_guest_os_name(self, vm):
        return constants.GUEST_OS_CHOICES.get(vm.guest_os)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.VirtualMachine
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'guest_os', 'guest_os_name', 'cores', 'cores_per_socket', 'ram', 'disk', 'disks',
            'runtime_state', 'template', 'cluster', 'networks', 'datastore', 'folder',
            'template_name', 'cluster_name', 'datastore_name', 'folder_name', 'ports',
            'guest_power_state', 'tools_state', 'tools_installed',
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'guest_os', 'template', 'cluster', 'networks', 'datastore', 'folder', 'ports',
            'name',
        )
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'disk', 'runtime_state', 'guest_power_state', 'tools_installed',
        )
        extra_kwargs = dict(
            cores={'required': False},
            cores_per_socket={'required': False},
            ram={'required': False},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def get_fields(self):
        """
        When basic mode is activated, user is not allowed
        to select placement attributes for the new virtual machine.
        """
        fields = super(VirtualMachineSerializer, self).get_fields()

        if 'ram' in fields:
            fields['ram'].factor = 1024
            fields['ram'].units = 'GB'
            fields['ram'].min_value = 1024

        if 'disk' in fields:
            fields['disk'].factor = 1024
            fields['disk'].units = 'GB'

        if 'cores' in fields:
            fields['cores'].min_value = 1

        if 'cores_per_socket' in fields:
            fields['cores_per_socket'].min_value = 1

        if isinstance(self.instance, models.VirtualMachine):
            spl = self.instance.service_project_link
            options = spl.service.settings.options

            if 'cores' in fields:
                fields['cores'].max_value = options.get('max_cpu')

            if 'ram' in fields and 'max_ram' in options:
                fields['ram'].max_value = options.get('max_ram')

            if 'cores_per_socket' in fields and 'max_cores_per_socket' in options:
                fields['cores_per_socket'].max_value = options.get('max_cores_per_socket')

            if 'disk' in fields and 'max_disk' in options:
                fields['disk'].max_value = get_int_or_none(options, 'max_disk')

            if 'disk' in fields and 'max_disk_total' in options:
                if fields['disk'].max_value:
                    fields['disk'].max_value = min(
                        get_int_or_none(options, 'max_disk_total'),
                        get_int_or_none(options, 'max_disk')
                    )
                else:
                    fields['disk'].max_value = get_int_or_none(options, 'max_disk_total')

        if not is_basic_mode():
            return fields

        try:
            method = self.context['view'].request.method
        except (KeyError, AttributeError):
            return fields

        if method == 'POST':
            read_only_fields = 'cluster', 'networks', 'datastore', 'folder'
            for field in read_only_fields:
                fields[field].read_only = True

        return fields

    def _validate_attributes(self, attrs):
        template_attrs = {'cores', 'cores_per_socket', 'ram', 'disk', 'guest_os'}
        template = attrs.get('template')
        missing_attributes = template_attrs - set(attrs.keys()) - {'disk'}
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
        """
        Validate vCPU specification against service limits.
        """
        actual_cpu = attrs.get('cores')
        max_cpu = options.get('max_cpu')
        if actual_cpu and max_cpu and actual_cpu > max_cpu:
            raise serializers.ValidationError('Requested amount of CPU exceeds offering limit.')

        cores_per_socket = attrs.get('cores_per_socket')
        if cores_per_socket and actual_cpu % cores_per_socket != 0:
            raise serializers.ValidationError('Number of CPU cores should be multiple of cores per socket.')

        max_cores_per_socket = options.get('max_cores_per_socket')
        if cores_per_socket and max_cores_per_socket and cores_per_socket > max_cores_per_socket:
            raise serializers.ValidationError('Requested amount of cores per socket exceeds offering limit.')

    def _validate_ram(self, attrs, options):
        """
        Validate RAM specification against service limits.
        """
        actual_ram = attrs.get('ram')
        max_ram = options.get('max_ram')
        if actual_ram and actual_ram < 1024:
            raise serializers.ValidationError('Requested amount of RAM is too small.')

        if actual_ram and max_ram and actual_ram > max_ram:
            raise serializers.ValidationError('Requested amount of RAM exceeds offering limit.')

    def _validate_disk(self, attrs, options):
        """
        Validate storage specification against service limits.
        """
        template = attrs.get('template')
        max_disk = get_int_or_none(options, 'max_disk')
        actual_disk = template.disk if template else 0
        if actual_disk and max_disk and actual_disk > max_disk:
            raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

        max_disk_total = get_int_or_none(options, 'max_disk_total')
        if actual_disk and max_disk_total and actual_disk > max_disk_total:
            raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

    def _validate_limits(self, attrs):
        """
        Validate hardware specification against service limits.
        """
        if self.instance:
            spl = self.instance.service_project_link
        else:
            spl = attrs['service_project_link']

        options = spl.service.settings.options
        self._validate_cpu(attrs, options)
        self._validate_ram(attrs, options)
        self._validate_disk(attrs, options)
        return attrs

    def _validate_cluster(self, attrs):
        """
        If basic mode is activated, match cluster by customer and service.
        Otherwise use cluster provided by user if it is allowed.
        Finally, use default cluster from service settings.
        """
        spl = attrs['service_project_link']

        if is_basic_mode():
            customer = spl.project.customer
            try:
                cluster = models.Cluster.objects.filter(
                    settings=spl.service.settings,
                    customercluster__customer=customer).get()
            except ObjectDoesNotExist:
                return self._fallback_to_default_cluster(attrs)
            except MultipleObjectsReturned:
                raise serializers.ValidationError(
                    'There are multiple clusters assigned to the current customer.')
            else:
                attrs['cluster'] = cluster
            return attrs

        cluster = attrs.get('cluster')

        if cluster:
            if cluster.settings != spl.service.settings:
                raise serializers.ValidationError('This cluster is not available for this service.')

            if not cluster.customercluster_set.filter(customer=spl.project.customer).exists():
                raise serializers.ValidationError('This cluster is not available for this customer.')
        else:
            return self._fallback_to_default_cluster(attrs)
        return attrs

    def _fallback_to_default_cluster(self, attrs):
        spl = attrs['service_project_link']
        default_cluster_label = spl.service.settings.options.get('default_cluster_label')

        if not default_cluster_label:
            raise serializers.ValidationError('Default cluster is not defined for this service.')
        try:
            attrs['cluster'] = models.Cluster.objects.filter(
                settings=spl.service.settings,
                name=default_cluster_label).get()
            return attrs
        except models.Cluster.DoesNotExist:
            raise serializers.ValidationError('Default cluster is not defined for this service.')

    def _validate_folder(self, attrs):
        """
        If basic mode is activated, match folder by customer and service.
        Otherwise use folder provided by user if it is allowed.
        """
        spl = attrs['service_project_link']

        if is_basic_mode():
            customer = spl.project.customer
            try:
                folder = models.Folder.objects.filter(
                    settings=spl.service.settings,
                    customerfolder__customer=customer).get()
            except ObjectDoesNotExist:
                raise serializers.ValidationError(
                    'There is no folder assigned to the current customer.')
            except MultipleObjectsReturned:
                raise serializers.ValidationError(
                    'There are multiple folders assigned to the current customer.')
            else:
                attrs['folder'] = folder
            return attrs

        folder = attrs.get('folder')

        if folder:
            if folder.settings != spl.service.settings:
                raise serializers.ValidationError('This folder is not available for this service.')

            if not folder.customerfolder_set.filter(customer=spl.project.customer).exists():
                raise serializers.ValidationError('This folder is not available for this customer.')
        return attrs

    def _validate_datastore(self, attrs):
        """
        If basic mode is activated, match datastore by customer and service.
        Otherwise use datastore provided by user if it is valid with respect to its size.
        """
        spl = attrs['service_project_link']
        template = attrs.get('template')

        if is_basic_mode():
            customer = spl.project.customer
            datastore = models.Datastore.objects.filter(
                settings=spl.service.settings,
                customerdatastore__customer=customer
            ).order_by('-free_space').first()
            if not datastore:
                raise serializers.ValidationError(
                    'There is no datastore assigned to the current customer.')
            elif template and template.disk > datastore.free_space:
                raise serializers.ValidationError(
                    'There is no datastore with enough free space available for current customer.')
            else:
                attrs['datastore'] = datastore
            return attrs

        datastore = attrs.get('datastore')

        if datastore:
            if datastore.settings != spl.service.settings:
                raise serializers.ValidationError('This datastore is not available for this service.')

            if not datastore.customerdatastore_set.filter(customer=spl.project.customer).exists():
                raise serializers.ValidationError('This datastore is not available for this customer.')

            if template and template.disk > datastore.free_space:
                raise serializers.ValidationError(
                    'There is no datastore with enough free space available for current customer.')
        return attrs

    def _validate_networks(self, attrs):
        """
        If basic mode is activated, match network by customer and service.
        Otherwise use networks provided by user if it is valid with respect to its size.
        """
        spl = attrs['service_project_link']

        if is_basic_mode():
            customer = spl.project.customer
            try:
                network = models.Network.objects.filter(
                    settings=spl.service.settings,
                    customernetwork__customer=customer).get()
            except ObjectDoesNotExist:
                raise serializers.ValidationError(
                    'There is no network assigned to the current customer.')
            except MultipleObjectsReturned:
                raise serializers.ValidationError(
                    'There are multiple networks assigned to the current customer.')
            else:
                attrs['networks'] = [network]
            return attrs

        networks = attrs.get('networks', [])

        for network in networks:
            if network.settings != spl.service.settings:
                raise serializers.ValidationError('This network is not available for this service.')

            if not network.customernetwork_set.filter(customer=spl.project.customer).exists():
                raise serializers.ValidationError('This network is not available for this customer.')

        return attrs

    validators_pipeline = [
        _validate_attributes,
        _validate_cluster,
        _validate_limits,
        _validate_datastore,
        _validate_folder,
        _validate_networks,
    ]

    def validate(self, attrs):
        attrs = super(VirtualMachineSerializer, self).validate(attrs)

        if self.instance:
            self._validate_limits(attrs)
        else:
            for validator in self.validators_pipeline:
                attrs = validator(self, attrs)

        return attrs

    def create(self, validated_data):
        networks = validated_data.pop('networks', [])
        vm = super(VirtualMachineSerializer, self).create(validated_data)
        vm.networks.add(*networks)
        return vm


class PortSerializer(structure_serializers.BaseResourceSerializer):
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

    vm_name = serializers.ReadOnlyField(source='vm.name')
    vm_uuid = serializers.ReadOnlyField(source='vm.uuid')
    network_name = serializers.ReadOnlyField(source='network.name')

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Port
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'mac_address', 'vm', 'vm_uuid', 'vm_name', 'network', 'network_name',
        )
        # Virtual Ethernet adapter name is generated automatically by VMware itself,
        # therefore it's not editable by user
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'name', 'vm', 'mac_address',
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'network',
        )
        extra_kwargs = dict(
            vm={
                'view_name': 'vmware-virtual-machine-detail',
                'lookup_field': 'uuid',
            },
            network={
                'view_name': 'vmware-network-detail',
                'lookup_field': 'uuid',
            },
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def get_fields(self):
        # Skip metadata tweak on update because network is protected from update
        fields = super(PortSerializer, self).get_fields()
        if 'network' in fields and self.instance:
            network_field = fields['network']
            network_field.display_name_field = 'name'
            network_field.query_params = {
                'customer_pair_uuid': self.instance.customer.uuid.hex,
                'settings_uuid': self.instance.service_settings.uuid.hex,
            }
        return fields

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        vm = self.context['view'].get_object()
        attrs['vm'] = vm
        attrs['service_project_link'] = vm.service_project_link

        if not models.CustomerNetworkPair.objects.filter(
            customer=vm.customer,
            network=attrs['network'],
        ).exists():
            raise serializers.ValidationError('This network is not available for this customer.')

        return super(PortSerializer, self).validate(attrs)

    def create(self, validated_data):
        # Virtual Adapter is updated with actual name when pulling is performed
        validated_data['name'] = 'New virtual Adapter'
        return super(PortSerializer, self).create(validated_data)


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

    vm_uuid = serializers.ReadOnlyField(source='vm.uuid')
    vm_name = serializers.ReadOnlyField(source='vm.name')

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Disk
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'size', 'vm', 'vm_uuid', 'vm_name'
        )
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + (
            'size', 'name'
        )
        # Virtual disk name is generated automatically by VMware itself,
        # therefore it's not editable by user
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'name', 'vm',
        )
        extra_kwargs = dict(
            vm={
                'view_name': 'vmware-virtual-machine-detail',
                'lookup_field': 'uuid',
            },
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def get_fields(self):
        fields = super(DiskSerializer, self).get_fields()
        fields['size'].factor = 1024
        fields['size'].units = 'GB'
        fields['size'].min_value = 1024

        if isinstance(self.instance, models.VirtualMachine):
            options = self.instance.service_settings.options

            max_disk = get_int_or_none(options, 'max_disk')
            if max_disk:
                fields['size'].max_value = max_disk

            max_disk_total = get_int_or_none(options, 'max_disk_total')
            if max_disk_total:
                remaining_quota = max_disk_total - self.instance.total_disk
                if fields['size'].max_value:
                    fields['size'].max_value = min(max_disk, remaining_quota)
                else:
                    fields['size'].max_value = remaining_quota
        return fields

    def _validate_size(self, vm, attrs):
        options = vm.service_project_link.service.settings.options

        actual_disk = attrs.get('size')
        if actual_disk < 1024:
            raise serializers.ValidationError('Requested amount of disk is too small.')

        max_disk = get_int_or_none(options, 'max_disk')
        if actual_disk and max_disk and actual_disk > max_disk:
            raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

        max_disk_total = get_int_or_none(options, 'max_disk_total')
        if actual_disk and max_disk_total:
            remaining_quota = max_disk_total - vm.total_disk
            if actual_disk > remaining_quota:
                raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

    def create(self, validated_data):
        # Virtual disk is updated with actual name when pulling is performed
        validated_data['name'] = 'New disk'
        return super(DiskSerializer, self).create(validated_data)

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

    def get_fields(self):
        fields = super(DiskExtendSerializer, self).get_fields()
        fields['size'].factor = 1024
        fields['size'].units = 'GB'

        if isinstance(self.instance, models.Disk):
            fields['size'].min_value = self.instance.size + 1024
            options = self.instance.service_settings.options
            max_disk = get_int_or_none(options, 'max_disk')
            if max_disk:
                fields['size'].max_value = max_disk

            max_disk_total = get_int_or_none(options, 'max_disk_total')
            if max_disk_total:
                remaining_quota = max_disk_total - self.instance.vm.total_disk + self.instance.size
                if max_disk:
                    fields['size'].max_value = min(max_disk, remaining_quota)
                else:
                    fields['size'].max_value = remaining_quota

        return fields

    def validate_size(self, value):
        if value <= self.instance.size:
            raise serializers.ValidationError(
                _('Disk size should be greater than %s') % self.instance.size)

        options = self.instance.service_settings.options
        max_disk = get_int_or_none(options, 'max_disk')
        if max_disk and value > max_disk:
            raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

        max_disk_total = get_int_or_none(options, 'max_disk_total')
        if max_disk_total:
            remaining_quota = max_disk_total - self.instance.vm.total_disk + self.instance.size
            if value > remaining_quota:
                raise serializers.ValidationError('Requested amount of disk exceeds offering limit.')

        return value


class TemplateSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Template
        fields = (
            'url', 'uuid', 'name', 'description', 'created', 'modified',
            'guest_os', 'guest_os_name', 'cores', 'cores_per_socket', 'ram', 'disk',
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


class FolderSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Folder
        fields = (
            'url', 'uuid', 'name',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
