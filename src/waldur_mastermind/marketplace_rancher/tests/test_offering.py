from rest_framework import test
from rest_framework.serializers import ValidationError

from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    RANCHER_OFFERING,
    OrderStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import STORAGE_MODE_DYNAMIC
from waldur_mastermind.marketplace_rancher.const import DEPLOYMENT_MODE_MANAGED
from waldur_openstack.tests import (
    factories as openstack_factories,
)
from waldur_openstack.tests import (
    fixtures as openstack_fixtures,
)
from waldur_rancher.tests import factories as rancher_factories


class Request:
    def __init__(self, request_user):
        self.user = request_user


class ClusterTenantLimitsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        service_settings = rancher_factories.RancherServiceSettingsFactory()
        self.openstack_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, scope=self.fixture.tenant.service_settings
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
            type=RANCHER_OFFERING, scope=service_settings
        )
        self.offering.plugin_options.update(
            {
                "deployment_mode": DEPLOYMENT_MODE_MANAGED,
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
                "openstack_offering_uuid_list": [self.openstack_offering.uuid.hex],
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
                "worker_nodes_data_volume_size": 51200,  # 50 GB in MB
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
        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_order(self.order, Request(self.fixture.staff))

        details = str(context.exception.detail)

        self.assertIn("The requested total cores limit", details)
        self.assertNotIn("MB", details)

    def test_cluster_creation_fails_when_ram_limit_exceeded(self):
        self.offering.plugin_options.update(
            {
                "managed_rancher_tenant_max_ram": self.ram_size * 4 + 1,
            }
        )
        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_order(self.order, Request(self.fixture.staff))

        details = str(context.exception.detail)
        self.assertIn("The requested total ram limit", details)
        self.assertIn("MB", details)

    def test_cluster_creation_fails_when_disk_limit_exceeded(self):
        self.offering.plugin_options.update(
            {
                "managed_rancher_tenant_max_disk": 100,
            }
        )
        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_order(self.order, Request(self.fixture.staff))

        details = str(context.exception.detail)
        self.assertIn("The requested total storage limit", details)
        self.assertIn("MB", details)

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

        with self.assertRaises(ValidationError) as context:
            marketplace_utils.validate_order(self.order, Request(self.fixture.staff))

        details = str(context.exception.detail)
        self.assertIn("The requested total storage limit", details)
        self.assertIn("MB", details)
