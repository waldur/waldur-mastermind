from rest_framework import test

from waldur_core.structure.tests.factories import ProjectFactory, SshPublicKeyFactory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_rancher import PLUGIN_NAME
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)
from waldur_rancher import models as rancher_models
from waldur_rancher.tests import factories as rancher_factories


class OrderProcessedTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()

    def test_resource_is_created_when_order_is_processed(self):
        service_settings = rancher_factories.RancherServiceSettingsFactory()
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, scope=service_settings
        )

        openstack_tenant_factories.FlavorFactory(
            settings=self.fixture.openstack_tenant_service_settings,
            ram=1024 * 8,
            cores=8,
        )
        image = openstack_tenant_factories.ImageFactory(
            settings=self.fixture.openstack_tenant_service_settings
        )
        openstack_tenant_factories.SecurityGroupFactory(
            name='default', settings=self.fixture.openstack_tenant_service_settings
        )
        service_settings.options['base_image_name'] = image.name
        service_settings.save()

        ssh_public_key = SshPublicKeyFactory(user=self.fixture.staff)
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=offering,
            attributes={
                'name': 'name',
                'tenant_settings': openstack_tenant_factories.OpenStackTenantServiceSettingsFactory.get_url(
                    self.fixture.openstack_tenant_service_settings
                ),
                'project': ProjectFactory.get_url(self.fixture.project),
                'ssh_public_key': SshPublicKeyFactory.get_url(ssh_public_key),
                'nodes': [
                    {
                        'subnet': openstack_tenant_factories.SubNetFactory.get_url(
                            self.fixture.subnet
                        ),
                        'system_volume_size': 1024,
                        'memory': 1,
                        'cpu': 1,
                        'roles': ['controlplane', 'etcd', 'worker'],
                    }
                ],
            },
            state=marketplace_models.Order.States.EXECUTING,
        )
        marketplace_utils.process_order(order, self.fixture.staff)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(name='name').exists()
        )
        self.assertTrue(rancher_models.Cluster.objects.filter(name='name').exists())
