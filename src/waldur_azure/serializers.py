import uuid

from django.db import transaction
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers

from waldur_azure.utils import generate_password, generate_username, hash_string
from waldur_core.core.models import SshPublicKey
from waldur_core.structure import serializers as structure_serializers
from waldur_core.structure.models import ServiceSettings

from . import models


class AzureServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = (
            'tenant_id',
            'client_id',
            'client_secret',
            'subscription_id',
        )

    tenant_id = serializers.CharField(
        source='options.tenant_id',
        label=_('Azure Active Directory tenant ID or domain'),
    )

    client_id = serializers.CharField(
        source='options.client_id',
        label=_('Azure Active Directory application client ID'),
    )

    client_secret = serializers.CharField(
        source='options.client_secret',
        label=_('Azure Active Directory application secret'),
    )

    subscription_id = serializers.CharField(
        source='options.subscription_id', label=_('Azure subscription ID')
    )


class ImageSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Image
        view_name = 'azure-image-detail'
        fields = ('url', 'uuid', 'publisher', 'name', 'sku', 'version')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class SizeSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Size
        view_name = 'azure-size-detail'
        fields = (
            'url',
            'uuid',
            'name',
            'max_data_disk_count',
            'memory_in_mb',
            'number_of_cores',
            'os_disk_size_in_mb',
            'resource_disk_size_in_mb',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class LocationSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Location
        view_name = 'azure-location-detail'
        fields = ('url', 'uuid', 'name', 'latitude', 'longitude')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class BaseResourceSerializer(structure_serializers.BaseResourceSerializer):
    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + ('name',)
        )


class ResourceGroupSerializer(BaseResourceSerializer):
    location = serializers.HyperlinkedRelatedField(
        view_name='azure-location-detail',
        lookup_field='uuid',
        queryset=models.Location.objects.all(),
    )

    class Meta(BaseResourceSerializer.Meta):
        model = models.ResourceGroup
        view_name = 'azure-resource-group-detail'
        fields = BaseResourceSerializer.Meta.fields + ('location',)


class BaseResourceGroupSerializer(BaseResourceSerializer):
    resource_group_name = serializers.ReadOnlyField(source='resource_group.name')
    location_name = serializers.ReadOnlyField(source='resource_group.location.name')

    resource_group = serializers.HyperlinkedRelatedField(
        view_name='azure-resource-group-detail', lookup_field='uuid', read_only=True,
    )

    location = serializers.HyperlinkedRelatedField(
        view_name='azure-location-detail',
        lookup_field='uuid',
        queryset=models.Location.objects.all(),
        write_only=True,
    )


class VirtualMachineSerializer(
    structure_serializers.VirtualMachineSerializer, BaseResourceGroupSerializer
):

    image = serializers.HyperlinkedRelatedField(
        view_name='azure-image-detail',
        lookup_field='uuid',
        queryset=models.Image.objects.all(),
    )

    size = serializers.HyperlinkedRelatedField(
        view_name='azure-size-detail',
        lookup_field='uuid',
        queryset=models.Size.objects.all(),
    )

    ssh_public_key = serializers.HyperlinkedRelatedField(
        view_name='sshpublickey-detail',
        lookup_field='uuid',
        queryset=SshPublicKey.objects.all(),
        required=False,
        allow_null=True,
        write_only=True,
        source='ssh_key',
    )

    size_name = serializers.ReadOnlyField(source='size.name')
    image_name = serializers.ReadOnlyField(source='image.name')

    class Meta(structure_serializers.VirtualMachineSerializer.Meta):
        model = models.VirtualMachine
        view_name = 'azure-virtualmachine-detail'
        fields = structure_serializers.VirtualMachineSerializer.Meta.fields + (
            'image',
            'size',
            'user_data',
            'runtime_state',
            'start_time',
            'cores',
            'ram',
            'disk',
            'image_name',
            'location',
            'resource_group',
            'username',
            'password',
            'resource_group_name',
            'location_name',
            'image_name',
            'size_name',
        )
        protected_fields = (
            structure_serializers.VirtualMachineSerializer.Meta.protected_fields
            + ('image', 'size', 'user_data', 'name')
        )
        read_only_fields = (
            structure_serializers.VirtualMachineSerializer.Meta.read_only_fields
            + (
                'runtime_state',
                'start_time',
                'cores',
                'ram',
                'disk',
                'image_name',
                'username',
                'password',
            )
        )

    @transaction.atomic
    def create(self, validated_data):
        vm_name = validated_data['name']
        service_settings: ServiceSettings = validated_data['service_settings']
        project = validated_data['project']
        size = validated_data['size']
        location = validated_data.pop('location')

        resource_group_name = f'group-{uuid.uuid4().hex[:4]}-{vm_name}'
        storage_account_name = 'storage{}'.format(hash_string(vm_name.lower(), 14))
        network_name = 'net{}'.format(vm_name)
        subnet_name = 'subnet{}'.format(vm_name)
        nic_name = 'nic{}'.format(vm_name)
        config_name = 'ipconf{}'.format(vm_name)
        public_ip_name = 'pubip{}'.format(vm_name)
        security_group_name = 'NSG{}'.format(vm_name)

        resource_group = models.ResourceGroup.objects.create(
            service_settings=service_settings,
            project=project,
            name=resource_group_name,
            location=location,
        )

        models.StorageAccount.objects.create(
            service_settings=service_settings,
            project=project,
            name=storage_account_name,
            resource_group=resource_group,
        )

        network = models.Network.objects.create(
            service_settings=service_settings,
            project=project,
            resource_group=resource_group,
            name=network_name,
            cidr='10.0.0.0/16',
        )

        subnet = models.SubNet.objects.create(
            service_settings=service_settings,
            project=project,
            resource_group=resource_group,
            name=subnet_name,
            cidr='10.0.0.0/24',
            network=network,
        )

        public_ip = models.PublicIP.objects.create(
            service_settings=service_settings,
            project=project,
            resource_group=resource_group,
            location=location,
            name=public_ip_name,
        )

        security_group = models.SecurityGroup.objects.create(
            service_settings=service_settings,
            project=project,
            resource_group=resource_group,
            name=security_group_name,
        )

        nic = models.NetworkInterface.objects.create(
            service_settings=service_settings,
            project=project,
            resource_group=resource_group,
            name=nic_name,
            subnet=subnet,
            config_name=config_name,
            public_ip=public_ip,
            security_group=security_group,
        )

        validated_data['ram'] = size.memory_in_mb
        validated_data['cores'] = size.number_of_cores
        validated_data['disk'] = size.os_disk_size_in_mb + size.resource_disk_size_in_mb

        validated_data['network_interface'] = nic
        validated_data['resource_group'] = resource_group
        validated_data['username'] = generate_username()
        validated_data['password'] = generate_password()

        return super(VirtualMachineSerializer, self).create(validated_data)


class PublicIPSerializer(BaseResourceSerializer):

    location = serializers.HyperlinkedRelatedField(
        view_name='azure-location-detail',
        lookup_field='uuid',
        queryset=models.Location.objects.all(),
    )

    resource_group = serializers.HyperlinkedRelatedField(
        view_name='azure-resource-group-detail',
        lookup_field='uuid',
        queryset=models.ResourceGroup.objects.all(),
    )

    class Meta(BaseResourceSerializer.Meta):
        model = models.PublicIP
        view_name = 'azure-public-ip-detail'
        fields = BaseResourceSerializer.Meta.fields + ('location', 'resource_group',)


class SQLServerSerializer(BaseResourceGroupSerializer):
    class Meta(BaseResourceGroupSerializer.Meta):
        model = models.SQLServer
        view_name = 'azure-sql-server-detail'
        fields = BaseResourceGroupSerializer.Meta.fields + (
            'resource_group',
            'location',
            'username',
            'password',
            'storage_mb',
            'username',
            'password',
            'fqdn',
            'resource_group_name',
            'location_name',
        )
        read_only_fields = BaseResourceGroupSerializer.Meta.read_only_fields + (
            'username',
            'password',
            'fqdn',
        )

    @transaction.atomic
    def create(self, validated_data):
        service_settings = validated_data['service_settings']
        project = validated_data['project']
        location = validated_data.pop('location')

        resource_group_name = 'group{}'.format(uuid.uuid4().hex)

        resource_group = models.ResourceGroup.objects.create(
            service_settings=service_settings,
            project=project,
            name=resource_group_name,
            location=location,
        )
        validated_data['resource_group'] = resource_group
        validated_data['username'] = generate_username()
        validated_data['password'] = generate_password()

        return super(SQLServerSerializer, self).create(validated_data)


class SQLDatabaseSerializer(BaseResourceSerializer):
    resource_group_name = serializers.ReadOnlyField(source='server.resource_group.name')
    location_name = serializers.ReadOnlyField(
        source='server.resource_group.location.name'
    )
    server_name = serializers.ReadOnlyField(source='server.name')
    server_uuid = serializers.ReadOnlyField(source='server.uuid')

    server = serializers.HyperlinkedRelatedField(
        view_name='azure-sql-server-detail',
        lookup_field='uuid',
        queryset=models.SQLServer.objects.all(),
    )

    class Meta(BaseResourceSerializer.Meta):
        model = models.SQLDatabase
        view_name = 'azure-sql-database-detail'
        fields = BaseResourceSerializer.Meta.fields + (
            'server',
            'charset',
            'collation',
            'resource_group_name',
            'location_name',
            'server_name',
            'server_uuid',
        )


class SQLDatabaseCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.SQLDatabase
        fields = ('name', 'description')

    def create(self, validated_data):
        server = self.context['view'].get_object()
        validated_data['server'] = server
        validated_data['service_settings'] = server.service_settings
        validated_data['project'] = server.project
        return super(SQLDatabaseCreateSerializer, self).create(validated_data)
