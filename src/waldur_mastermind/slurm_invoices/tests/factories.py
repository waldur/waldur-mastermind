import factory
from rest_framework.reverse import reverse

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories

from .. import models


class SlurmServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    class Meta:
        model = structure_models.ServiceSettings

    type = 'SLURM'


class SlurmPackageFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.SlurmPackage

    service_settings = factory.SubFactory(SlurmServiceSettingsFactory)

    @classmethod
    def get_url(cls, package=None, action=None):
        if package is None:
            package = SlurmPackageFactory()
        url = 'http://testserver' + reverse(
            'slurm-package-detail', kwargs={'uuid': package.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('slurm-package-list')
