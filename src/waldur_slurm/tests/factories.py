import factory
from factory import fuzzy

from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class SlurmServiceFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.SlurmService

    settings = factory.SubFactory(structure_factories.ServiceSettingsFactory, type='SLURM')
    customer = factory.SubFactory(structure_factories.CustomerFactory)

    @classmethod
    def get_url(cls, service=None, action=None):
        if service is None:
            service = SlurmServiceFactory()
        url = 'http://testserver' + reverse('slurm-detail', kwargs={'uuid': service.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('slurm-list')


class SlurmServiceProjectLinkFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.SlurmServiceProjectLink

    service = factory.SubFactory(SlurmServiceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, link=None):
        if link is None:
            link = SlurmServiceProjectLinkFactory()
        return 'http://testserver' + reverse('slurm-spl-detail', kwargs={'pk': link.id})


class AllocationFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.Allocation

    name = factory.Sequence(lambda n: 'allocation%s' % n)
    backend_id = factory.Sequence(lambda n: 'allocation-id%s' % n)
    service_project_link = factory.SubFactory(SlurmServiceProjectLinkFactory)

    state = models.Allocation.States.OK
    cpu_limit = fuzzy.FuzzyInteger(1000, 8000, step=100)
    gpu_limit = fuzzy.FuzzyInteger(1000, 8000, step=100)
    ram_limit = fuzzy.FuzzyInteger(100, 1000, step=100)
    deposit_limit = fuzzy.FuzzyInteger(100, 1000, step=100)

    @classmethod
    def get_url(cls, allocation=None, action=None):
        if allocation is None:
            allocation = AllocationFactory()
        url = 'http://testserver' + reverse('slurm-allocation-detail', kwargs={'uuid': allocation.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('slurm-allocation-list')


class AllocationUsageFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.AllocationUsage

    username = factory.Sequence(lambda n: 'john%s' % n)
    allocation = factory.SubFactory(AllocationFactory)

    year = factory.Iterator(range(2012, 2016))
    month = factory.Iterator(range(1, 13))

    cpu_usage = fuzzy.FuzzyInteger(1000, 8000, step=100)
    gpu_usage = fuzzy.FuzzyInteger(1000, 8000, step=100)
    ram_usage = fuzzy.FuzzyInteger(100, 1000, step=100)
    deposit_usage = fuzzy.FuzzyInteger(100, 1000, step=100)

    @classmethod
    def get_url(cls, allocation_usage=None):
        if allocation_usage is None:
            allocation_usage = AllocationUsageFactory()
        return 'http://testserver' + reverse('slurm-allocation-usage-detail', kwargs={
            'uuid': allocation_usage.uuid.hex
        })

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('slurm-allocation-usage-list')
