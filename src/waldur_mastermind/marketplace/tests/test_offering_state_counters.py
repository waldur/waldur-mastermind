from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace.enums import (
    OfferingUserStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories, fixtures


@ddt
class OfferingStateCountersTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.url = factories.OfferingFactory.get_url(self.offering, "state_counters")

    # --- Permission tests ---

    def test_offering_owner_can_access(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_access(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("user", "admin", "manager")
    def test_non_owner_cannot_access(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # --- Resource state counter tests ---

    def test_resource_state_counters(self):
        """Test that resources are correctly grouped by state."""
        self.client.force_authenticate(self.fixture.offering_owner)
        # Fixture creates one resource in CREATING state by default
        # Create additional resources in different states
        factories.ResourceFactory(offering=self.offering, state=ResourceStates.OK)
        factories.ResourceFactory(offering=self.offering, state=ResourceStates.OK)
        factories.ResourceFactory(offering=self.offering, state=ResourceStates.ERRED)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        resources = response.data["resources"]
        state_map = {item["state"]: item["count"] for item in resources}

        self.assertEqual(state_map.get("OK"), 2)
        self.assertEqual(state_map.get("Erred"), 1)
        self.assertEqual(state_map.get("Creating"), 1)  # from fixture

    # --- Offering user state counter tests ---

    def test_user_state_counters(self):
        """Test that offering users are correctly grouped by state."""
        self.client.force_authenticate(self.fixture.offering_owner)

        factories.OfferingUserFactory(
            offering=self.offering, state=OfferingUserStates.OK
        )
        factories.OfferingUserFactory(
            offering=self.offering, state=OfferingUserStates.OK
        )
        factories.OfferingUserFactory(
            offering=self.offering,
            state=OfferingUserStates.CREATION_REQUESTED,
            username=None,
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        users = response.data["users"]
        state_map = {item["state"]: item["count"] for item in users}

        self.assertEqual(state_map.get("OK"), 2)
        self.assertEqual(state_map.get("Requested"), 1)

    # --- Edge cases ---

    def test_empty_offering(self):
        """An offering with no resources/users returns empty arrays."""
        self.client.force_authenticate(self.fixture.staff)
        empty_offering = factories.OfferingFactory()
        url = factories.OfferingFactory.get_url(empty_offering, "state_counters")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["resources"], [])
        self.assertEqual(response.data["users"], [])

    def test_response_structure(self):
        """Verify the response has the expected top-level keys."""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(self.url)
        self.assertIn("resources", response.data)
        self.assertIn("users", response.data)
        self.assertIsInstance(response.data["resources"], list)
        self.assertIsInstance(response.data["users"], list)

    def test_does_not_count_other_offerings(self):
        """Resources from other offerings should not be counted."""
        self.client.force_authenticate(self.fixture.offering_owner)
        other_offering = factories.OfferingFactory()
        factories.ResourceFactory(offering=other_offering, state=ResourceStates.OK)

        response = self.client.get(self.url)
        resources = response.data["resources"]
        # Only the fixture's own resource (CREATING) should appear
        total = sum(item["count"] for item in resources)
        self.assertEqual(total, 1)
