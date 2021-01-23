from random import randint

import factory
from django.urls import reverse
from libcloud.compute.types import NodeState

from waldur_azure import models
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories


class AzureServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    class Meta:
        model = structure_models.ServiceSettings

    type = 'Azure'
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class AzureServiceFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.AzureService

    settings = factory.SubFactory(AzureServiceSettingsFactory)
    customer = factory.SelfAttribute('settings.customer')

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = AzureServiceFactory()
        url = 'http://testserver' + reverse(
            'azure-detail', kwargs={'uuid': service.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('azure-list')


class AzureServiceProjectLinkFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.AzureServiceProjectLink

    service = factory.SubFactory(AzureServiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, spl=None, action=None):
        if spl is None:
            spl = AzureServiceProjectLinkFactory()
        url = 'http://testserver' + reverse('azure-spl-detail', kwargs={'pk': spl.pk})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('azure-spl-list')


class LocationFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Location

    settings = factory.SubFactory(AzureServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'region-%s' % n)
    backend_id = factory.Sequence(lambda n: 'region-%s' % n)

    @classmethod
    def get_url(cls, spl=None, action=None):
        if spl is None:
            spl = LocationFactory()
        url = 'http://testserver' + reverse(
            'azure-location-detail', kwargs={'uuid': spl.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('azure-location-list')


class SizeFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Size

    settings = factory.SubFactory(AzureServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'size-%s' % n)
    backend_id = factory.Sequence(lambda n: 'size-%s' % n)

    max_data_disk_count = factory.fuzzy.FuzzyInteger(1, 8, step=2)
    memory_in_mb = factory.fuzzy.FuzzyInteger(1024, 102400, step=1024)
    number_of_cores = factory.fuzzy.FuzzyInteger(1, 8, step=2)
    os_disk_size_in_mb = factory.fuzzy.FuzzyInteger(1024, 102400, step=1024)
    resource_disk_size_in_mb = factory.fuzzy.FuzzyInteger(1024, 102400, step=1024)

    @classmethod
    def get_url(cls, spl=None, action=None):
        if spl is None:
            spl = SizeFactory()
        url = 'http://testserver' + reverse(
            'azure-size-detail', kwargs={'uuid': spl.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('azure-size-list')


class ImageFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Image

    settings = factory.SubFactory(AzureServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'img-%s' % n)
    backend_id = factory.Sequence(lambda n: 'img-%s' % n)

    sku = factory.Sequence(lambda n: 'sku-%s' % n)
    publisher = factory.Sequence(lambda n: 'pub-%s' % n)
    version = factory.Sequence(lambda n: 'v-%s' % n)

    @classmethod
    def get_url(cls, spl=None, action=None):
        if spl is None:
            spl = ImageFactory()
        url = 'http://testserver' + reverse(
            'azure-image-detail', kwargs={'uuid': spl.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('azure-image-list')


class ResourceGroupFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.ResourceGroup

    name = factory.Sequence(lambda n: 'rg-%s' % n)
    backend_id = factory.Sequence(lambda n: 'rg-%s' % n)
    service_project_link = factory.SubFactory(AzureServiceProjectLinkFactory)
    location = factory.SubFactory(LocationFactory)


class NetworkFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Network

    name = factory.Sequence(lambda n: 'net-%s' % n)
    backend_id = factory.Sequence(lambda n: 'net-%s' % n)
    service_project_link = factory.SubFactory(AzureServiceProjectLinkFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    cidr = '10.0.0.0/16'


class SubNetFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.SubNet

    name = factory.Sequence(lambda n: 'subnet-%s' % n)
    backend_id = factory.Sequence(lambda n: 'subnet-%s' % n)
    service_project_link = factory.SubFactory(AzureServiceProjectLinkFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    cidr = '10.0.0.0/24'
    network = factory.SubFactory(NetworkFactory)


class NetworkInterfaceFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.NetworkInterface

    name = factory.Sequence(lambda n: 'nic-%s' % n)
    backend_id = factory.Sequence(lambda n: 'nic-%s' % n)
    service_project_link = factory.SubFactory(AzureServiceProjectLinkFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    subnet = factory.SubFactory(SubNetFactory)
    config_name = factory.Sequence(lambda n: 'conf-%s' % n)


class PublicIPFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.PublicIP

    name = factory.Sequence(lambda n: 'floating_ip%s' % n)
    backend_id = factory.Sequence(lambda n: 'floating_ip%s' % n)
    location = factory.SubFactory(LocationFactory)
    service_project_link = factory.SubFactory(AzureServiceProjectLinkFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    ip_address = factory.LazyAttribute(
        lambda o: '.'.join('%s' % randint(0, 255) for _ in range(4))  # noqa: S311
    )


class VirtualMachineFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.VirtualMachine

    name = factory.Sequence(lambda n: 'vm-%s' % n)
    backend_id = factory.Sequence(lambda n: 'vm-%s' % n)
    service_project_link = factory.SubFactory(AzureServiceProjectLinkFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    size = factory.SubFactory(SizeFactory)
    image = factory.SubFactory(ImageFactory)
    network_interface = factory.SubFactory(NetworkInterfaceFactory)

    state = models.VirtualMachine.States.OK
    runtime_state = NodeState.RUNNING
    cores = factory.fuzzy.FuzzyInteger(1, 8, step=2)
    ram = factory.fuzzy.FuzzyInteger(1024, 10240, step=1024)
    disk = factory.fuzzy.FuzzyInteger(1024, 102400, step=1024)

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = VirtualMachineFactory()
        url = 'http://testserver' + reverse(
            'azure-virtualmachine-detail', kwargs={'uuid': instance.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('azure-virtualmachine-list')


class SQLServerFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.SQLServer

    name = factory.Sequence(lambda n: 'sql-%s' % n)
    backend_id = factory.Sequence(lambda n: 'sql-%s' % n)
    service_project_link = factory.SubFactory(AzureServiceProjectLinkFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    state = models.SQLServer.States.OK

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = VirtualMachineFactory()
        url = 'http://testserver' + reverse(
            'azure-sql-server-detail', kwargs={'uuid': instance.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('azure-sql-server-list')
