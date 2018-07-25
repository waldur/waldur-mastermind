from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class RijkscloudFixture(ProjectFixture):
    @cached_property
    def service_settings(self):
        return factories.ServiceSettingsFactory(customer=self.customer)

    @cached_property
    def service(self):
        return factories.ServiceFactory(customer=self.customer, settings=self.service_settings)

    @cached_property
    def network(self):
        return factories.NetworkFactory(settings=self.service_settings)

    @cached_property
    def subnet(self):
        return factories.SubNetFactory(settings=self.service_settings, network=self.network)

    @cached_property
    def internal_ip(self):
        return factories.InternalIPFactory(settings=self.service_settings, subnet=self.subnet)

    @cached_property
    def floating_ip(self):
        return factories.FloatingIPFactory(settings=self.service_settings)

    @cached_property
    def spl(self):
        return factories.ServiceProjectLinkFactory(project=self.project, service=self.service)

    @cached_property
    def instance(self):
        return factories.InstanceFactory(
            service_project_link=self.spl,
            internal_ip=self.internal_ip,
            floating_ip=self.floating_ip,
        )

    @cached_property
    def flavor(self):
        return factories.FlavorFactory(settings=self.service_settings)
