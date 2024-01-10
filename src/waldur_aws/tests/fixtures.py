from django.utils.functional import cached_property

from waldur_core.structure.tests.factories import ServiceSettingsFactory
from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class AWSFixture(ProjectFixture):
    @cached_property
    def region(self):
        return factories.RegionFactory()

    @cached_property
    def image(self):
        return factories.ImageFactory(region=self.region)

    @cached_property
    def size(self):
        size = factories.SizeFactory()
        size.regions.add(self.region)
        return size

    @cached_property
    def service_settings(self):
        return ServiceSettingsFactory(type="Amazon", customer=self.customer)

    @cached_property
    def instance(self):
        return factories.InstanceFactory(
            service_settings=self.service_settings,
            project=self.project,
            region=self.region,
        )
