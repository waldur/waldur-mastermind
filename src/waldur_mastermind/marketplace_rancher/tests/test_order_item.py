from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests.factories import SshPublicKeyFactory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
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


class OrderItemProcessedTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()

    def test_resource_is_created_when_order_item_is_processed(self):
        service = rancher_factories.RancherServiceFactory(
            customer=self.fixture.customer
        )
        spl = rancher_factories.RancherServiceProjectLinkFactory(
            project=self.fixture.project, service=service
        )
        service_settings = spl.service.settings
        offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, scope=service_settings
        )

        openstack_tenant_factories.FlavorFactory(
            settings=self.fixture.spl.service.settings, ram=1024 * 8, cores=8
        )
        image = openstack_tenant_factories.ImageFactory(
            settings=self.fixture.spl.service.settings
        )
        openstack_tenant_factories.SecurityGroupFactory(
            name='default', settings=self.fixture.spl.service.settings
        )
        service_settings.options['base_image_name'] = image.name
        service_settings.save()

        order = marketplace_factories.OrderFactory(
            project=self.fixture.project, created_by=self.fixture.owner
        )
        ssh_public_key = SshPublicKeyFactory(user=self.fixture.staff)
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=offering,
            attributes={
                'name': 'name',
                'tenant_settings': openstack_tenant_factories.OpenStackTenantServiceSettingsFactory.get_url(
                    self.fixture.spl.service.settings
                ),
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
        )
        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(name='name').exists()
        )
        self.assertTrue(rancher_models.Cluster.objects.filter(name='name').exists())
