from django.utils.functional import cached_property

from waldur_core.core.enums import CoreStates
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_openstack.models import Tenant
from waldur_openstack.tests import factories as openstack_factories
from waldur_rancher import enums

from . import factories


class RancherFixture(ProjectFixture):
    def __init__(self):
        super().__init__()
        self.node

    @cached_property
    def settings(self):
        return factories.RancherServiceSettingsFactory(customer=self.customer)

    @cached_property
    def tenant(self) -> Tenant:
        return openstack_factories.TenantFactory(project=self.project)

    @cached_property
    def cluster(self):
        return factories.ClusterFactory(
            settings=self.settings,
            service_settings=self.settings,
            project=self.project,
            state=CoreStates.OK,
            tenant=self.tenant,
            name="my-cluster",
        )

    @cached_property
    def instance(self):
        return openstack_factories.InstanceFactory(
            service_settings=self.tenant.service_settings,
            tenant=self.tenant,
            project=self.project,
            state=CoreStates.OK,
        )

    @cached_property
    def node(self):
        return factories.NodeFactory(
            cluster=self.cluster,
            instance=self.instance,
            state=CoreStates.OK,
        )

    @cached_property
    def cluster_owner_role(self):
        return factories.RoleTemplateFactory(
            name="cluster-owner",
            display_name="Cluster Owner",
            settings=self.settings,
        )

    @cached_property
    def cluster_member_role(self):
        return factories.RoleTemplateFactory(
            name="cluster-member",
            display_name="Cluster Member",
            settings=self.settings,
        )

    @cached_property
    def project_owner_role(self):
        return factories.RoleTemplateFactory(
            name="project-owner",
            display_name="Project Owner",
            settings=self.settings,
            scope_type=enums.RoleScopeType.PROJECT,
        )
