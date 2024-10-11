from django.urls import reverse
from rest_framework import status, test

from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    PlanFactory,
    ResourceFactory,
)
from waldur_mastermind.marketplace_openstack import CORES_TYPE, RAM_TYPE
from waldur_openstack.models import Tenant
from waldur_openstack.tests.factories import VolumeTypeFactory
from waldur_openstack.tests.fixtures import OpenStackFixture
from waldur_openstack.utils import volume_type_name_to_quota_name


class MigrationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()

    def test_migration_is_created(self):
        offering = OfferingFactory(scope=self.fixture.settings)
        plan = PlanFactory(offering=offering)
        resource = ResourceFactory(offering=offering, scope=self.fixture.tenant)
        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "src_resource": resource.uuid.hex,
                "dst_offering": offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_tenant_has_credentials(self):
        offering = OfferingFactory(scope=self.fixture.settings)
        plan = PlanFactory(offering=offering)
        resource = ResourceFactory(offering=offering, scope=self.fixture.tenant)
        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "src_resource": resource.uuid.hex,
                "dst_offering": offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
            },
        )
        dst_resource_uuid = response.data["dst_resource_uuid"]
        dst_resource = Resource.objects.get(uuid=dst_resource_uuid)
        tenant: Tenant = dst_resource.scope
        self.assertNotEqual(tenant.user_username, "")
        self.assertNotEqual(tenant.user_password, "")

    def test_volume_types_mapping(self):
        offering = OfferingFactory(scope=self.fixture.settings)
        plan = PlanFactory(offering=offering)
        volume_type1 = VolumeTypeFactory(settings=self.fixture.settings)
        self.fixture.tenant.volume_types.add(volume_type1)
        volume_type2 = VolumeTypeFactory(settings=self.fixture.settings)
        resource = ResourceFactory(
            offering=offering,
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
                "dst_offering": offering.uuid.hex,
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
        offering = OfferingFactory(scope=self.fixture.settings)
        plan = PlanFactory(offering=offering)
        resource = ResourceFactory(offering=offering, scope=self.fixture.tenant)
        self.fixture.security_group_rule
        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            reverse("openstack-migrations-list"),
            {
                "src_resource": resource.uuid.hex,
                "dst_offering": offering.uuid.hex,
                "dst_plan": plan.uuid.hex,
            },
        )
        dst_resource_uuid = response.data["dst_resource_uuid"]
        dst_resource = Resource.objects.get(uuid=dst_resource_uuid)
        tenant: Tenant = dst_resource.scope
        self.assertEqual(tenant.security_groups.get().rules.count(), 1)
