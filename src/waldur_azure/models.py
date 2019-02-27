from __future__ import unicode_literals

from django.db import models
from model_utils import FieldTracker

from waldur_azure import validators
from waldur_core.core import models as core_models
from waldur_core.quotas.fields import CounterQuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models


class AzureService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project, related_name='azure_services', through='AzureServiceProjectLink')

    @classmethod
    def get_url_name(cls):
        return 'azure'

    class Quotas(QuotaModelMixin.Quotas):
        vm_count = CounterQuotaField(
            target_models=lambda: [VirtualMachine],
            path_to_scope='service_project_link.service'
        )


class AzureServiceProjectLink(structure_models.ServiceProjectLink):
    service = models.ForeignKey(AzureService)

    @classmethod
    def get_url_name(cls):
        return 'azure-spl'


class Location(core_models.CoordinatesMixin,
               structure_models.ServiceProperty):

    enabled = models.BooleanField(
        default=True,
        help_text='Indicates whether location is available for resource group.'
    )

    class Meta:
        ordering = ['name']

    @classmethod
    def get_url_name(cls):
        return 'azure-location'


class Image(structure_models.ServiceProperty):
    publisher = models.CharField(max_length=255)
    sku = models.CharField(max_length=255)
    version = models.CharField(max_length=255)

    class Meta(structure_models.ServiceProperty.Meta):
        ordering = ['publisher', 'name', 'sku']

    @classmethod
    def get_url_name(cls):
        return 'azure-image'


class Size(structure_models.ServiceProperty):
    max_data_disk_count = models.PositiveIntegerField()
    memory_in_mb = models.PositiveIntegerField()
    number_of_cores = models.PositiveIntegerField()
    os_disk_size_in_mb = models.PositiveIntegerField()
    resource_disk_size_in_mb = models.PositiveIntegerField()

    class Meta(structure_models.ServiceProperty.Meta):
        ordering = ['number_of_cores', 'memory_in_mb']

    @classmethod
    def get_url_name(cls):
        return 'azure-size'


class BaseResource(core_models.RuntimeStateMixin, structure_models.NewResource):
    service_project_link = models.ForeignKey(AzureServiceProjectLink)

    class Meta(object):
        abstract = True


class ResourceGroup(BaseResource):
    name = models.CharField(max_length=90, validators=[validators.ResourceGroupNameValidator])
    location = models.ForeignKey(Location)

    @classmethod
    def get_url_name(cls):
        return 'azure-resource-group'


class BaseResourceGroupModel(BaseResource):
    resource_group = models.ForeignKey(ResourceGroup)

    class Meta(object):
        abstract = True


class StorageAccount(BaseResourceGroupModel):
    name = models.CharField(max_length=24, validators=[validators.StorageAccountNameValidator])


class Network(BaseResourceGroupModel):
    name = models.CharField(max_length=64, validators=[validators.NetworkingNameValidator])
    cidr = models.CharField(max_length=32)


class SubNet(BaseResourceGroupModel):
    name = models.CharField(max_length=80, validators=[validators.NetworkingNameValidator])
    network = models.ForeignKey(Network)
    cidr = models.CharField(max_length=32)


class SecurityGroup(BaseResourceGroupModel):
    name = models.CharField(max_length=80, validators=[validators.NetworkingNameValidator])


class NetworkInterface(BaseResourceGroupModel):
    name = models.CharField(max_length=80, validators=[validators.NetworkingNameValidator])
    subnet = models.ForeignKey(SubNet)
    config_name = models.CharField(max_length=255)
    public_ip = models.ForeignKey('PublicIP', on_delete=models.SET_NULL, null=True, blank=True)
    security_group = models.ForeignKey(SecurityGroup, on_delete=models.SET_NULL, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, protocol='IPv4', default=None)
    tracker = FieldTracker()


class PublicIP(BaseResourceGroupModel):
    name = models.CharField(max_length=80, validators=[validators.NetworkingNameValidator])
    location = models.ForeignKey(Location)
    ip_address = models.GenericIPAddressField(null=True, blank=True, protocol='IPv4', default=None)
    tracker = FieldTracker()

    @classmethod
    def get_url_name(cls):
        return 'azure-public-ip'


class VirtualMachine(structure_models.VirtualMachine):
    service_project_link = models.ForeignKey(
        AzureServiceProjectLink, related_name='virtualmachines', on_delete=models.PROTECT)
    resource_group = models.ForeignKey(ResourceGroup)
    size = models.ForeignKey(Size)
    image = models.ForeignKey(Image)
    ssh_key = models.ForeignKey(core_models.SshPublicKey, null=True, blank=True)
    network_interface = models.ForeignKey(NetworkInterface)
    name = models.CharField(max_length=15, validators=[validators.VirtualMachineNameValidator])
    username = models.CharField(max_length=32, validators=[validators.VirtualMachineUsernameValidator])
    password = models.CharField(max_length=72, validators=validators.VirtualMachinePasswordValidators)
    user_data = models.TextField(
        blank=True,
        max_length=87380,
        help_text='Additional data that will be added to instance on provisioning')

    @property
    def internal_ips(self):
        return [self.network_interface.ip_address]

    @property
    def external_ips(self):
        public_ip = self.network_interface.public_ip
        return public_ip and [public_ip.ip_address] or []

    @classmethod
    def get_url_name(cls):
        return 'azure-virtualmachine'

    def get_access_url_name(self):
        return 'azure-virtualmachine-rdp'


class SQLServer(BaseResourceGroupModel):
    name = models.CharField(max_length=80, validators=[validators.SQLServerNameValidator])
    username = models.CharField(max_length=50, validators=[validators.SQLServerUsernameValidator])
    password = models.CharField(max_length=128, validators=validators.SQLServerPasswordValidators)
    storage_mb = models.PositiveIntegerField(null=True, validators=validators.SQLServerStorageValidators)
    fqdn = models.TextField(null=True, blank=True)
    tracker = FieldTracker()

    @classmethod
    def get_url_name(cls):
        return 'azure-sql-server'


class SQLDatabase(BaseResource):
    server = models.ForeignKey(SQLServer)
    charset = models.CharField(max_length=255, blank=True, null=True, default='utf8')
    collation = models.CharField(max_length=255, blank=True, null=True, default='utf8_general_ci')
    tracker = FieldTracker()

    @classmethod
    def get_url_name(cls):
        return 'azure-sql-database'
