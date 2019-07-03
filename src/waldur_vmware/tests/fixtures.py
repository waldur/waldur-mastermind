from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class VMwareFixture(ProjectFixture):

    @cached_property
    def settings(self):
        return factories.VMwareServiceSettingsFactory(customer=self.customer)

    @cached_property
    def service(self):
        return factories.VMwareServiceFactory(customer=self.customer, settings=self.settings)

    @cached_property
    def spl(self):
        return factories.VMwareServiceProjectLinkFactory(service=self.service, project=self.project)

    @cached_property
    def cluster(self):
        return factories.ClusterFactory(settings=self.settings)

    @cached_property
    def customer_cluster(self):
        return factories.CustomerClusterFactory(cluster=self.cluster, customer=self.customer)

    @cached_property
    def template(self):
        return factories.TemplateFactory(settings=self.settings)

    @cached_property
    def virtual_machine(self):
        return factories.VirtualMachineFactory(
            service_project_link=self.spl,
            template=self.template,
            cluster=self.cluster,
        )

    @cached_property
    def disk(self):
        return factories.DiskFactory(
            vm=self.virtual_machine,
            service_project_link=self.spl,
        )
