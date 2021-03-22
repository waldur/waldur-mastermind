from django.utils.functional import cached_property

from waldur_core.structure.tests.fixtures import ProjectFixture

from . import factories


class VMwareFixture(ProjectFixture):
    def __init__(self):
        super(VMwareFixture, self).__init__()
        self.customer_cluster
        self.customer_network
        self.customer_datastore
        self.customer_folder

    @cached_property
    def settings(self):
        return factories.VMwareServiceSettingsFactory(customer=self.customer)

    @cached_property
    def cluster(self):
        return factories.ClusterFactory(settings=self.settings)

    @cached_property
    def customer_cluster(self):
        return factories.CustomerClusterFactory(
            cluster=self.cluster, customer=self.customer
        )

    @cached_property
    def network(self):
        return factories.NetworkFactory(settings=self.settings)

    @cached_property
    def customer_network(self):
        return factories.CustomerNetworkFactory(
            network=self.network, customer=self.customer
        )

    @cached_property
    def customer_network_pair(self):
        return factories.CustomerNetworkPairFactory(
            network=self.network, customer=self.customer
        )

    @cached_property
    def datastore(self):
        return factories.DatastoreFactory(settings=self.settings)

    @cached_property
    def customer_datastore(self):
        return factories.CustomerDatastoreFactory(
            datastore=self.datastore, customer=self.customer
        )

    @cached_property
    def folder(self):
        return factories.FolderFactory(settings=self.settings)

    @cached_property
    def customer_folder(self):
        return factories.CustomerFolderFactory(
            folder=self.folder, customer=self.customer
        )

    @cached_property
    def template(self):
        return factories.TemplateFactory(settings=self.settings)

    @cached_property
    def virtual_machine(self):
        return factories.VirtualMachineFactory(
            service_settings=self.settings,
            project=self.project,
            template=self.template,
            cluster=self.cluster,
        )

    @cached_property
    def disk(self):
        return factories.DiskFactory(
            vm=self.virtual_machine,
            service_settings=self.settings,
            project=self.project,
        )
