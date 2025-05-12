from django.urls import reverse
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    PlanFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
)
from waldur_openstack.models import SubNet, Tenant
from waldur_openstack.tests.factories import (
    NetworkFactory,
    RouterFactory,
    SubNetFactory,
    VolumeTypeFactory,
)
from waldur_openstack.tests.fixtures import OpenStackFixture
from waldur_openstack.utils import volume_type_name_to_quota_name
from waldur_openstack_replication.models import Migration


class MigrationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()
        self.offering = OfferingFactory(scope=self.fixture.settings)
        self.offering.plugin_options["storage_mode"] = STORAGE_MODE_DYNAMIC
        self.offering.save()

    def test_migration_is_created(self):
        plan = PlanFactory(offering=self.offering)
        resource = ResourceFactory(offering=self.offering, scope=self.fixture.tenant)
        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "name": "replicated vpc",
                "src_resource": resource.uuid.hex,
                "dst_offering": self.offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["dst_resource_name"], "replicated vpc")

    def test_tenant_has_credentials(self):
        plan = PlanFactory(offering=self.offering)
        resource = ResourceFactory(offering=self.offering, scope=self.fixture.tenant)
        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "src_resource": resource.uuid.hex,
                "dst_offering": self.offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
            },
        )
        dst_resource_uuid = response.data["dst_resource_uuid"]
        dst_resource = Resource.objects.get(uuid=dst_resource_uuid)
        tenant: Tenant = dst_resource.scope
        self.assertNotEqual(tenant.user_username, "")
        self.assertNotEqual(tenant.user_password, "")

    def test_volume_types_mapping(self):
        plan = PlanFactory(offering=self.offering)
        volume_type1 = VolumeTypeFactory(settings=self.fixture.settings)
        self.fixture.tenant.volume_types.add(volume_type1)
        volume_type2 = VolumeTypeFactory(settings=self.fixture.settings)
        resource = ResourceFactory(
            offering=self.offering,
            scope=self.fixture.tenant,
            limits={
                CORES_TYPE: 1,
                RAM_TYPE: 1 * 1024,
                volume_type_name_to_quota_name(volume_type1.name): 10,
            },
        )
        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "src_resource": resource.uuid.hex,
                "dst_offering": self.offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
                "mappings": {
                    "volume_types": [
                        {
                            "src_type_uuid": volume_type1.uuid.hex,
                            "dst_type_uuid": volume_type2.uuid.hex,
                        }
                    ],
                    "subnets": [],
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        dst_resource_uuid = response.data["dst_resource_uuid"]
        dst_resource = Resource.objects.get(uuid=dst_resource_uuid)
        self.assertEqual(
            10, dst_resource.limits[volume_type_name_to_quota_name(volume_type2.name)]
        )

    def test_security_group_rules_are_replicated(self):
        plan = PlanFactory(offering=self.offering)
        resource = ResourceFactory(offering=self.offering, scope=self.fixture.tenant)
        self.fixture.security_group_rule
        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "src_resource": resource.uuid.hex,
                "dst_offering": self.offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
            },
        )
        dst_resource_uuid = response.data["dst_resource_uuid"]
        dst_resource = Resource.objects.get(uuid=dst_resource_uuid)
        tenant: Tenant = dst_resource.scope
        self.assertEqual(tenant.security_groups.get().rules.count(), 1)

    def test_order_is_created_on_migration_success(self):
        src_resource = ResourceFactory(offering=self.offering)
        dst_resource = ResourceFactory(offering=self.offering)
        migration = Migration.objects.create(
            created_by=self.fixture.staff,
            src_resource=src_resource,
            dst_resource=dst_resource,
        )
        self.client.force_login(self.fixture.staff)

        # Change state to OK and check order creation
        migration.state = CoreStates.OK
        migration.save()
        self.assertTrue(Order.objects.filter(resource=dst_resource).exists())

    def test_order_is_created_on_migration_failure(self):
        src_resource = ResourceFactory(offering=self.offering)
        dst_resource = ResourceFactory(offering=self.offering)
        migration = Migration.objects.create(
            created_by=self.fixture.staff,
            src_resource=src_resource,
            dst_resource=dst_resource,
        )
        self.client.force_login(self.fixture.staff)

        # Change state to ERRED and check order creation
        migration.state = CoreStates.ERRED
        migration.save()
        self.assertTrue(
            Order.objects.filter(
                resource=dst_resource, state=OrderStates.ERRED
            ).exists()
        )

    def test_migration_with_selected_networks(self):
        plan = PlanFactory(offering=self.offering)
        resource = ResourceFactory(offering=self.offering, scope=self.fixture.tenant)

        # Create two networks and subnets in the source tenant
        network1 = NetworkFactory(tenant=self.fixture.tenant)
        SubNetFactory(network=network1, cidr="192.168.1.0/24")
        network2 = NetworkFactory(tenant=self.fixture.tenant)
        SubNetFactory(network=network2, cidr="10.0.0.0/24")

        # Create a router and add static routes
        RouterFactory(
            tenant=self.fixture.tenant,
            routes=[
                {"destination": "192.168.1.0/24", "nexthop": "172.17.8.100"},
                {"destination": "10.0.0.0/24", "nexthop": "172.17.8.100"},
            ],
        )

        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "src_resource": resource.uuid.hex,
                "dst_offering": self.offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
                "mappings": {
                    "networks": [network1.uuid.hex],  # Select only network1
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        dst_resource_uuid = response.data["dst_resource_uuid"]
        dst_resource = Resource.objects.get(uuid=dst_resource_uuid)
        dst_tenant: Tenant = dst_resource.scope

        # Assert that only the selected network is created in the destination tenant
        self.assertEqual(dst_tenant.networks.count(), 1)
        self.assertEqual(dst_tenant.networks.first().name, network1.name)

        # Assert that the subnet is also created
        self.assertEqual(SubNet.objects.filter(tenant=dst_tenant).count(), 1)

        # Assert that only the static route associated with the selected network is created
        dst_router = dst_tenant.routers.first()
        self.assertEqual(len(dst_router.routes), 1)
        self.assertEqual(dst_router.routes[0]["destination"], "192.168.1.0/24")

    def test_migration_with_all_networks(self):
        plan = PlanFactory(offering=self.offering)
        resource = ResourceFactory(offering=self.offering, scope=self.fixture.tenant)

        # Create two networks and subnets in the source tenant
        network1 = NetworkFactory(tenant=self.fixture.tenant)
        SubNetFactory(network=network1, cidr="192.168.1.0/24")
        network2 = NetworkFactory(tenant=self.fixture.tenant)
        SubNetFactory(network=network2, cidr="10.0.0.0/24")

        # Create a router and add static routes
        RouterFactory(
            tenant=self.fixture.tenant,
            routes=[
                {"destination": "192.168.1.0/24", "nexthop": "172.17.8.100"},
                {"destination": "10.0.0.0/24", "nexthop": "172.17.8.100"},
            ],
        )

        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "src_resource": resource.uuid.hex,
                "dst_offering": self.offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        dst_resource_uuid = response.data["dst_resource_uuid"]
        dst_resource = Resource.objects.get(uuid=dst_resource_uuid)
        dst_tenant: Tenant = dst_resource.scope

        # Assert that both networks are created in the destination tenant
        self.assertEqual(dst_tenant.networks.count(), 2)
        network_names = [network.name for network in dst_tenant.networks.all()]
        self.assertIn(network1.name, network_names)
        self.assertIn(network2.name, network_names)

        # Assert that both subnets are also created
        self.assertEqual(SubNet.objects.filter(tenant=dst_tenant).count(), 2)

        # Assert that both static routes are created
        dst_router = dst_tenant.routers.first()
        self.assertEqual(len(dst_router.routes), 2)
        routes = [route["destination"] for route in dst_router.routes]
        self.assertIn("192.168.1.0/24", routes)
        self.assertIn("10.0.0.0/24", routes)
