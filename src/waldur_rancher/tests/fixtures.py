from django.contrib.contenttypes.models import ContentType
from django.utils.functional import cached_property

from waldur_core.core.models import StateMixin
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_rancher import models

from . import factories


class RancherFixture(ProjectFixture):
    def __init__(self):
        super(RancherFixture, self).__init__()
        self.node

    @cached_property
    def settings(self):
        return factories.RancherServiceSettingsFactory(customer=self.customer)

    @cached_property
    def tenant_settings(self):
        return openstack_tenant_factories.OpenStackTenantServiceSettingsFactory(
            customer=self.customer
        )

    @cached_property
    def cluster(self):
        return factories.ClusterFactory(
            settings=self.settings,
            service_settings=self.settings,
            project=self.project,
            state=models.Cluster.States.OK,
            tenant_settings=self.tenant_settings,
            name='my-cluster',
        )

    @cached_property
    def instance(self):
        return openstack_tenant_factories.InstanceFactory(
            service_settings=self.tenant_settings,
            project=self.project,
            state=StateMixin.States.OK,
        )

    @cached_property
    def node(self):
        content_type = ContentType.objects.get_for_model(self.instance)
        return factories.NodeFactory(
            cluster=self.cluster,
            object_id=self.instance.id,
            content_type=content_type,
            state=models.Node.States.OK,
        )
