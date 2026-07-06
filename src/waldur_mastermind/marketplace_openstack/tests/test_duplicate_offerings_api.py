from rest_framework import status
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.tests.fixtures import OpenStackFixture

from .utils import BaseOpenStackTest

LIST_URL = reverse("marketplace-openstack-duplicate-offering-list")


class DuplicateOfferingsApiTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant

    def _offering(self, type=OPENSTACK_INSTANCE_OFFERING, scope=None, **kwargs):
        return marketplace_factories.OfferingFactory(
            type=type,
            scope=scope or self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=kwargs.pop("state", OfferingStates.ACTIVE),
            **kwargs,
        )

    def _get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(LIST_URL)

    def test_staff_sees_duplicate_group_with_keeper_flag(self):
        keeper = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.instance,
            project=self.fixture.project,
            offering=keeper,
        )
        self._offering()  # empty duplicate

        response = self._get(self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["tenant_id"], self.tenant.id)
        self.assertEqual(row["offering_type"], OPENSTACK_INSTANCE_OFFERING)
        self.assertEqual(row["recommended_keeper_id"], keeper.id)
        self.assertEqual(len(row["candidates"]), 2)
        keeper_candidate = next(c for c in row["candidates"] if c["id"] == keeper.id)
        self.assertTrue(keeper_candidate["is_recommended_keeper"])
        self.assertEqual(keeper_candidate["active_resources"], 1)

    def test_support_can_access(self):
        self._offering()
        self._offering()
        response = self._get(self.fixture.global_support)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_staff_is_forbidden(self):
        self._offering()
        self._offering()
        response = self._get(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_orphan_resources_are_counted(self):
        # fixture.instance is an Instance in the tenant with no marketplace
        # Resource — exactly an orphan the ambiguous offering can't heal.
        self.assertIsNotNone(self.fixture.instance)
        self._offering()
        self._offering()

        response = self._get(self.fixture.staff)

        row = response.data[0]
        self.assertEqual(row["orphan_count"], 1)

    def test_no_duplicates_returns_empty(self):
        self._offering()
        response = self._get(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_both_types_yield_two_rows(self):
        self._offering(type=OPENSTACK_INSTANCE_OFFERING)
        self._offering(type=OPENSTACK_INSTANCE_OFFERING)
        self._offering(type=OPENSTACK_VOLUME_OFFERING)
        self._offering(type=OPENSTACK_VOLUME_OFFERING)

        response = self._get(self.fixture.staff)

        self.assertEqual(len(response.data), 2)
        types = {row["offering_type"] for row in response.data}
        self.assertEqual(
            types, {OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING}
        )
