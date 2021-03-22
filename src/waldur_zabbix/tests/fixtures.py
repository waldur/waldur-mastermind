from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class ZabbixFixture(ProjectFixture):
    @cached_property
    def settings(self):
        return factories.ZabbixServiceSettingsFactory(customer=self.customer)
