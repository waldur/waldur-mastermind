import factory
from django.urls import reverse
from factory import fuzzy

from waldur_core.structure.tests.factories import ProjectFactory, ServiceSettingsFactory
from waldur_slurm import models


class SlurmServiceSettingsFactory(ServiceSettingsFactory):
    type = 'SLURM'


class AllocationFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Allocation

    name = factory.Sequence(lambda n: 'allocation%s' % n)
    backend_id = factory.Sequence(lambda n: 'allocation-id%s' % n)
    service_settings = factory.SubFactory(SlurmServiceSettingsFactory)
    project = factory.SubFactory(ProjectFactory)

    state = models.Allocation.States.OK
    cpu_limit = fuzzy.FuzzyInteger(1000, 8000, step=100)
    gpu_limit = fuzzy.FuzzyInteger(1000, 8000, step=100)
    ram_limit = fuzzy.FuzzyInteger(100, 1000, step=100)

    @classmethod
    def get_url(cls, allocation=None, action=None):
        if allocation is None:
            allocation = AllocationFactory()
        url = 'http://testserver' + reverse(
            'slurm-allocation-detail', kwargs={'uuid': allocation.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('slurm-allocation-list')


class AssociationFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Association

    allocation = factory.SubFactory(AllocationFactory)

    @classmethod
    def get_url(cls, association=None):
        if association is None:
            association = AssociationFactory()
        return 'http://testserver' + reverse(
            'slurm-association-detail', kwargs={'uuid': association.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('slurm-association-list')
