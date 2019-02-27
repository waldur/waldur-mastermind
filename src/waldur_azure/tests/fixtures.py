from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class AzureFixture(ProjectFixture):

    @cached_property
    def settings(self):
        return factories.AzureServiceSettingsFactory(customer=self.customer)

    @cached_property
    def service(self):
        return factories.AzureServiceFactory(customer=self.customer, settings=self.settings)

    @cached_property
    def spl(self):
        return factories.AzureServiceProjectLinkFactory(service=self.service, project=self.project)

    @cached_property
    def location(self):
        return factories.LocationFactory(settings=self.settings)

    @cached_property
    def image(self):
        return factories.ImageFactory(settings=self.settings)

    @cached_property
    def size(self):
        return factories.SizeFactory(settings=self.settings)

    @cached_property
    def resource_group(self):
        return factories.ResourceGroupFactory(
            location=self.location,
            service_project_link=self.spl
        )

    @cached_property
    def network(self):
        return factories.NetworkFactory(
            resource_group=self.resource_group,
            service_project_link=self.spl
        )

    @cached_property
    def subnet(self):
        return factories.SubNetFactory(
            resource_group=self.resource_group,
            service_project_link=self.spl,
            network=self.network,
        )

    @cached_property
    def network_interface(self):
        return factories.NetworkInterfaceFactory(
            resource_group=self.resource_group,
            service_project_link=self.spl,
            subnet=self.subnet,
        )

    @cached_property
    def public_ip(self):
        return factories.PublicIPFactory(
            resource_group=self.resource_group,
            service_project_link=self.spl,
            location=self.location,
        )

    @cached_property
    def virtual_machine(self):
        return factories.VirtualMachineFactory(
            service_project_link=self.spl,
            resource_group=self.resource_group,
            image=self.image,
            size=self.size,
            network_interface=self.network_interface,
        )

    @cached_property
    def sql_server(self):
        return factories.SQLServerFactory(
            service_project_link=self.spl,
            resource_group=self.resource_group,
        )
