from django.urls import reverse
from rest_framework import status, test

from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    PlanFactory,
    ResourceFactory,
)
from waldur_openstack.tests.fixtures import OpenStackFixture


class MigrationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = OpenStackFixture()

    def test_migrate(self):
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
                "mappings": {
                    "volume_types": [],
                    "subnets": [],
                },
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
