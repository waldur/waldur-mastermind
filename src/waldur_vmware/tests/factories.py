import datetime

from django.urls import reverse

import factory

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure import models as structure_models

from .. import models


class VMwareServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    class Meta(object):
        model = structure_models.ServiceSettings

    type = 'VMware'
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class VMwareServiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.VMwareService

    settings = factory.SubFactory(VMwareServiceSettingsFactory)
    customer = factory.SelfAttribute('settings.customer')

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = VMwareServiceFactory()
        url = 'http://testserver' + reverse('vmware-detail', kwargs={'uuid': service.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('vmware-list')


class VMwareServiceProjectLinkFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.VMwareServiceProjectLink

    service = factory.SubFactory(VMwareServiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, spl=None, action=None):
        if spl is None:
            spl = VMwareServiceProjectLinkFactory()
        url = 'http://testserver' + reverse('vmware-spl-detail', kwargs={'pk': spl.pk})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('vmware-spl-list')


class TemplateFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Template

    created = datetime.datetime.now()
    modified = datetime.datetime.now()
    settings = factory.SubFactory(VMwareServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'template-%s' % n)
    backend_id = factory.Sequence(lambda n: 'template-%s' % n)

    @classmethod
    def get_url(cls, template=None, action=None):
        template = template or TemplateFactory()
        url = 'http://testserver' + reverse('vmware-template-detail', kwargs={'uuid': template.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('vmware-template-list')


class ClusterFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Cluster

    settings = factory.SubFactory(VMwareServiceSettingsFactory)
    name = factory.Sequence(lambda n: 'cluster-%s' % n)
    backend_id = factory.Sequence(lambda n: 'cluster-%s' % n)

    @classmethod
    def get_url(cls, cluster=None, action=None):
        cluster = cluster or ClusterFactory()
        url = 'http://testserver' + reverse('vmware-cluster-detail', kwargs={'uuid': cluster.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('vmware-cluster-list')


class CustomerClusterFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.CustomerCluster

    customer = factory.SubFactory(structure_factories.CustomerFactory)
    cluster = factory.SubFactory(ClusterFactory)


class VirtualMachineFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.VirtualMachine

    name = factory.Sequence(lambda n: 'vm-%s' % n)
    backend_id = factory.Sequence(lambda n: 'vm-%s' % n)
    service_project_link = factory.SubFactory(VMwareServiceProjectLinkFactory)
    template = factory.SubFactory(TemplateFactory)
    cluster = factory.SubFactory(ClusterFactory)

    state = models.VirtualMachine.States.OK
    runtime_state = models.VirtualMachine.RuntimeStates.POWERED_ON
    cores = factory.fuzzy.FuzzyInteger(1, 8, step=2)
    ram = factory.fuzzy.FuzzyInteger(1024, 10240, step=1024)
    disk = factory.fuzzy.FuzzyInteger(1024, 102400, step=1024)

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = VirtualMachineFactory()
        url = 'http://testserver' + reverse('vmware-virtual-machine-detail', kwargs={'uuid': instance.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('vmware-virtual-machine-list')


class DiskFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Disk

    name = factory.Sequence(lambda n: 'disk-%s' % n)
    backend_id = factory.Sequence(lambda n: 'disk-%s' % n)
    service_project_link = factory.SubFactory(VMwareServiceProjectLinkFactory)

    state = models.Disk.States.OK
    size = factory.fuzzy.FuzzyInteger(1, 8, step=1)
    vm = factory.SubFactory(VirtualMachineFactory)

    @classmethod
    def get_url(cls, disk=None, action=None):
        disk = disk or DiskFactory()
        url = 'http://testserver' + reverse('vmware-disk-detail', kwargs={'uuid': disk.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('vmware-disk-list')
