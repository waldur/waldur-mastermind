from django.utils.functional import cached_property

from nodeconductor.structure.tests.fixtures import ProjectFixture

from . import factories


class SlurmFixture(ProjectFixture):
    @cached_property
    def service_settings(self):
        return factories.SlurmServiceSettingsFactory(customer=self.customer)

    @cached_property
    def package(self):
        return factories.SlurmPackageFactory(service_settings=self.service_settings)
