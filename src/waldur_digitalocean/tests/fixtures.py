from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class DigitalOceanFixture(ProjectFixture):
    @cached_property
    def size(self):
        size = factories.SizeFactory()
        size.regions.add(self.region)
        return size

    @cached_property
    def region(self):
        return factories.RegionFactory()

    @cached_property
    def image(self):
        image = factories.ImageFactory()
        image.regions.add(self.region)
        return image

    @cached_property
    def droplet(self):
        return factories.DropletFactory(service_project_link=self.spl)

    @cached_property
    def service(self):
        return factories.DigitalOceanServiceFactory(customer=self.customer)

    @cached_property
    def spl(self):
        return factories.DigitalOceanServiceProjectLinkFactory(
            service=self.service, project=self.project,
        )
