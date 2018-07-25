from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class AzureFixture(ProjectFixture):

    @cached_property
    def service(self):
        return factories.AzureServiceFactory(customer=self.customer)

    @cached_property
    def spl(self):
        return factories.AzureServiceProjectLinkFactory(service=self.service, project=self.project)

    @cached_property
    def virtual_machine(self):
        return factories.VirtualMachineFactory(service_project_link=self.spl)
