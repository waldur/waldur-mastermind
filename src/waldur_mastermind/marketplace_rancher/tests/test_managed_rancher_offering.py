from rest_framework import test

from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import STORAGE_MODE_DYNAMIC, TENANT_TYPE
from waldur_mastermind.marketplace_rancher import MANAGED_RANCHER_PLUGIN, PLUGIN_NAME
from waldur_openstack.tests import (
    factories as openstack_factories,
)
from waldur_openstack.tests import (
    fixtures as openstack_fixtures,
)
from waldur_rancher.tests import factories as rancher_factories


class ClusterTenantLimitsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        service_settings = rancher_factories.RancherServiceSettingsFactory()
        self.openstack_offering = marketplace_factories.OfferingFactory(
            type=TENANT_TYPE, scope=self.fixture.tenant.service_settings
        )
        self.rancher_offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, scope=service_settings
        )

        self.cpu_number = 8
        self.ram_size = 8
        self.flavor = openstack_factories.FlavorFactory(
            settings=self.fixture.tenant.service_settings,
            ram=self.ram_size * 1024,
            cores=self.cpu_number,
        )
        self.flavor.tenants.add(self.fixture.tenant)

        self.offering = marketplace_factories.OfferingFactory(
            type=MANAGED_RANCHER_PLUGIN, scope=self.rancher_offering
        )
        self.offering.plugin_options.update(
            {
                "managed_rancher_server_flavor_name": self.flavor.name,
                "managed_rancher_server_system_volume_size_gb": 50,
                "managed_rancher_server_system_volume_type_name": "prod",
                "managed_rancher_server_data_volume_size_gb": 50,
                "managed_rancher_server_data_volume_type_name": "prod",
                "managed_rancher_worker_system_volume_size_gb": 50,
                "managed_rancher_worker_system_volume_type_name": "prod",
                "managed_rancher_load_balancer_flavor_name": self.flavor.name,
                "managed_rancher_load_balancer_system_volume_size_gb": 50,
                "managed_rancher_load_balancer_system_volume_type_name": "prod",
                "managed_rancher_load_balancer_data_volume_size_gb": 50,
                "managed_rancher_load_balancer_data_volume_type_name": "prod",
            }
        )
        self.offering.secret_options.update(
            {
                "customer_uuid": self.fixture.project.customer.uuid.hex,
            }
        )
        self.offering.save()

        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            created_by=self.fixture.owner,
            offering=self.offering,
            attributes={
                "name": "managed-rancher-cluster-test",
                "worker_nodes_count": 1,
                "worker_nodes_flavor_name": self.flavor.name,
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
                "worker_nodes_data_volume_size": 50,
                "worker_nodes_data_volume_type_name": "prod3",
            },
            state=OrderStates.EXECUTING,
        )

    def test_cluster_creation_fails_when_cpu_limit_exceeded(self):
        self.offering.plugin_options.update(
            {
                "managed_rancher_tenant_max_cpu": self.cpu_number * 4 + 1,
            }
        )
        marketplace_utils.process_order(self.order, self.fixture.staff)

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertIn("The requested total cores limit", self.order.error_message)

    def test_cluster_creation_fails_when_ram_limit_exceeded(self):
        self.offering.plugin_options.update(
            {
                "managed_rancher_tenant_max_ram": self.ram_size * 4 + 1,
            }
        )
        marketplace_utils.process_order(self.order, self.fixture.staff)

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertIn("The requested total ram limit", self.order.error_message)

    def test_cluster_creation_fails_when_disk_limit_exceeded(self):
        self.offering.plugin_options.update(
            {
                "managed_rancher_tenant_max_disk": 100,
            }
        )
        marketplace_utils.process_order(self.order, self.fixture.staff)

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertIn("The requested total storage limit", self.order.error_message)

    def test_cluster_creation_fails_when_disk_limit_exceeded_for_dynamic_storage(self):
        self.offering.plugin_options.update(
            {
                "managed_rancher_tenant_max_disk": 100,
            }
        )
        self.openstack_offering.plugin_options.update(
            {
                "storage_mode": STORAGE_MODE_DYNAMIC,
            }
        )
        self.openstack_offering.save()

        marketplace_utils.process_order(self.order, self.fixture.staff)

        self.order.refresh_from_db()
        self.assertEqual(self.order.state, OrderStates.ERRED)
        self.assertIn("The requested total storage limit", self.order.error_message)
