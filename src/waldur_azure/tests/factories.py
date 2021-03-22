from random import randint

import factory
from django.urls import reverse
from libcloud.compute.types import NodeState

from waldur_azure import models
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.factories import ProjectFactory


class AzureServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    class Meta:
        model = structure_models.ServiceSettings

    type = 'Azure'
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class LocationFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Location

    settings = factory.SubFactory(AzureServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'region-%s' % n)
    backend_id = factory.Sequence(lambda n: 'region-%s' % n)

    @classmethod
    def get_url(cls, location=None, action=None):
        if location is None:
            location = LocationFactory()
        url = 'http://testserver' + reverse(
            'azure-location-detail', kwargs={'uuid': location.uuid.hex}
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
    def get_url(cls, size=None, action=None):
        if size is None:
            size = SizeFactory()
        url = 'http://testserver' + reverse(
            'azure-size-detail', kwargs={'uuid': size.uuid.hex}
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
    def get_url(cls, image=None, action=None):
        if image is None:
            image = ImageFactory()
        url = 'http://testserver' + reverse(
            'azure-image-detail', kwargs={'uuid': image.uuid.hex}
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
    service_settings = factory.SubFactory(AzureServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    location = factory.SubFactory(LocationFactory)


class NetworkFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Network

    name = factory.Sequence(lambda n: 'net-%s' % n)
    backend_id = factory.Sequence(lambda n: 'net-%s' % n)
    service_settings = factory.SubFactory(AzureServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    cidr = '10.0.0.0/16'


class SubNetFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.SubNet

    name = factory.Sequence(lambda n: 'subnet-%s' % n)
    backend_id = factory.Sequence(lambda n: 'subnet-%s' % n)
    service_settings = factory.SubFactory(AzureServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    cidr = '10.0.0.0/24'
    network = factory.SubFactory(NetworkFactory)


class NetworkInterfaceFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.NetworkInterface

    name = factory.Sequence(lambda n: 'nic-%s' % n)
    backend_id = factory.Sequence(lambda n: 'nic-%s' % n)
    service_settings = factory.SubFactory(AzureServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    subnet = factory.SubFactory(SubNetFactory)
    config_name = factory.Sequence(lambda n: 'conf-%s' % n)


class PublicIPFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.PublicIP

    name = factory.Sequence(lambda n: 'floating_ip%s' % n)
    backend_id = factory.Sequence(lambda n: 'floating_ip%s' % n)
    location = factory.SubFactory(LocationFactory)
    service_settings = factory.SubFactory(AzureServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
    resource_group = factory.SubFactory(ResourceGroupFactory)
    ip_address = factory.LazyAttribute(
        lambda o: '.'.join('%s' % randint(0, 255) for _ in range(4))  # noqa: S311
    )


class VirtualMachineFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.VirtualMachine

    name = factory.Sequence(lambda n: 'vm-%s' % n)
    backend_id = factory.Sequence(lambda n: 'vm-%s' % n)
    service_settings = factory.SubFactory(AzureServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
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
    service_settings = factory.SubFactory(AzureServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)
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
