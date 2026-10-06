from ddt import data, ddt, unpack
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
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
        # permissions.yaml grants ORDER.LIST to these roles; tests do not
        # import that file, so mirror the deployment here.
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS
        )

    def _get(self, user, action):
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering, action)
        return self.client.get(url)

    # --- Permission tests ---

    @data("state_counters", "component_stats")
    def test_offering_owner_can_access(self, action):
        response = self._get(self.fixture.offering_owner, action)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("state_counters", "component_stats")
    def test_staff_can_access(self, action):
        response = self._get(self.fixture.staff, action)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("state_counters", "component_stats")
    def test_offering_manager_can_access(self, action):
        response = self._get(self.fixture.offering_manager, action)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("state_counters", "component_stats")
    def test_custom_offering_role_with_list_orders_can_access(self, action):
        user = structure_factories.UserFactory()
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
        )
        role.add_permission(PermissionEnum.LIST_ORDERS)
        self.offering.add_user(user, role)

        response = self._get(user, action)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("state_counters", "component_stats")
    def test_custom_customer_role_with_statistics_only_can_access(self, action):
        # Statistics alone still reaches these, as it did before ORDER.LIST
        # was accepted too.
        user = structure_factories.UserFactory()
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(structure_models.Customer),
        )
        role.add_permission(PermissionEnum.GET_SERVICE_PROVIDER_STATISTICS)
        self.offering.customer.add_user(user, role)

        response = self._get(user, action)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("state_counters", "component_stats")
    def test_offering_role_without_list_orders_is_forbidden(self, action):
        # Connected to the offering, so the row is visible, but these need
        # statistics or ORDER.LIST rather than any offering-scoped role.
        user = structure_factories.UserFactory()
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
        )
        role.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.offering.add_user(user, role)

        response = self._get(user, action)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(
        *[
            (user, action)
            for user in ("user", "admin", "manager")
            for action in ("state_counters", "component_stats")
        ]
    )
    @unpack
    def test_non_owner_cannot_access(self, user, action):
        response = self._get(getattr(self.fixture, user), action)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("state_counters", "component_stats")
    def test_anonymous_cannot_access(self, action):
        url = factories.OfferingFactory.get_url(self.offering, action)
        response = self.client.get(url)
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
