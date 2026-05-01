from unittest import mock

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_keycloak import models
from waldur_keycloak.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class OfferingKeycloakGroupListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()
        self.url = factories.OfferingKeycloakGroupFactory.get_list_url()

    def test_staff_can_list_all_groups(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_anonymous_cannot_list_groups(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class OfferingKeycloakGroupRetrieveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()
        self.url = factories.OfferingKeycloakGroupFactory.get_url(
            self.fixture.keycloak_group
        )

    def test_staff_can_retrieve_group(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.fixture.keycloak_group.uuid.hex)


class OfferingKeycloakMembershipListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()
        self.url = factories.OfferingKeycloakMembershipFactory.get_list_url()

    def test_staff_can_list_all_memberships(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_anonymous_cannot_list_memberships(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class OfferingKeycloakMembershipCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()
        self.url = factories.OfferingKeycloakMembershipFactory.get_list_url()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    @mock.patch("waldur_keycloak.utils.send_membership_notification_email")
    def test_staff_can_create_membership(self, mock_send_email, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.create_group.return_value = {"id": "new-group-id"}
        mock_keycloak.find_user_by_username.return_value = {
            "id": "kc-user-id",
            "firstName": "Test",
            "lastName": "User",
        }

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            self.url,
            {
                "offering": marketplace_factories.OfferingFactory.get_url(
                    self.fixture.offering
                ),
                "role": factories.RoleFactory.get_url(self.fixture.offering_role),
                "username": "newuser",
                "email": "newuser@example.com",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        membership = models.OfferingKeycloakMembership.objects.get(
            uuid=response.data["uuid"]
        )
        self.assertEqual(membership.username, "newuser")
        self.assertEqual(membership.state, "active")
        mock_send_email.assert_called_once()

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    @mock.patch("waldur_keycloak.utils.send_membership_notification_email")
    def test_membership_stays_pending_when_user_not_in_keycloak(
        self, mock_send_email, mock_get_client
    ):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.create_group.return_value = {"id": "new-group-id"}
        mock_keycloak.find_user_by_username.return_value = None

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            self.url,
            {
                "offering": marketplace_factories.OfferingFactory.get_url(
                    self.fixture.offering
                ),
                "role": factories.RoleFactory.get_url(self.fixture.offering_role),
                "username": "pendinguser",
                "email": "pending@example.com",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        membership = models.OfferingKeycloakMembership.objects.get(
            uuid=response.data["uuid"]
        )
        self.assertEqual(membership.state, "pending")

    def test_cannot_create_membership_for_offering_without_keycloak(self):
        offering = marketplace_factories.OfferingFactory()
        role = factories.RoleFactory()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            self.url,
            {
                "offering": marketplace_factories.OfferingFactory.get_url(offering),
                "role": factories.RoleFactory.get_url(role),
                "username": "testuser2",
                "email": "test2@example.com",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    @mock.patch("waldur_keycloak.utils.send_membership_notification_email")
    def test_cannot_create_duplicate_membership(self, mock_send_email, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.find_user_by_username.return_value = None

        self.client.force_authenticate(user=self.fixture.staff)
        # The fixture already has a membership for "testuser"
        response = self.client.post(
            self.url,
            {
                "offering": marketplace_factories.OfferingFactory.get_url(
                    self.fixture.offering
                ),
                "role": factories.RoleFactory.get_url(self.fixture.offering_role),
                "username": "testuser",
                "email": "testuser@example.com",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OfferingKeycloakMembershipDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()
        self.url = factories.OfferingKeycloakMembershipFactory.get_url(
            self.fixture.keycloak_membership
        )

    def test_staff_can_delete_membership(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.OfferingKeycloakMembership.objects.filter(
                uuid=self.fixture.keycloak_membership.uuid
            ).exists()
        )

    def test_anonymous_cannot_delete_membership(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_delete_membership_calls_keycloak_remove_user(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.find_user_by_username.return_value = {
            "id": "kc-user-id",
        }
        mock_keycloak.get_group.return_value = {
            "id": self.fixture.keycloak_group.backend_id,
        }

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        mock_keycloak.remove_user_from_group.assert_called_once()


class OfferingKeycloakMembershipPermissionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()
        self.list_url = factories.OfferingKeycloakMembershipFactory.get_list_url()
        self.delete_url = factories.OfferingKeycloakMembershipFactory.get_url(
            self.fixture.keycloak_membership
        )
        # Grant MANAGE_RESOURCE_USERS permission to customer owner role
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_RESOURCE_USERS)

    def _get_create_payload(self):
        return {
            "offering": marketplace_factories.OfferingFactory.get_url(
                self.fixture.offering
            ),
            "role": factories.RoleFactory.get_url(self.fixture.offering_role),
            "username": "permtest-user",
            "email": "permtest@example.com",
        }

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    @mock.patch("waldur_keycloak.utils.send_membership_notification_email")
    def test_customer_owner_can_create_membership(
        self, mock_send_email, mock_get_client
    ):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.create_group.return_value = {"id": "new-group-id"}
        mock_keycloak.find_user_by_username.return_value = {
            "id": "kc-user-id",
            "firstName": "Test",
            "lastName": "User",
        }

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.list_url, self._get_create_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    @mock.patch("waldur_keycloak.utils.send_membership_notification_email")
    def test_regular_user_cannot_create_membership(
        self, mock_send_email, mock_get_client
    ):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.create_group.return_value = {"id": "new-group-id"}
        mock_keycloak.find_user_by_username.return_value = None

        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=regular_user)
        response = self.client.post(self.list_url, self._get_create_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @mock.patch("waldur_keycloak.utils.get_keycloak_client_for_offering")
    def test_customer_owner_can_delete_membership(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.find_user_by_username.return_value = {"id": "kc-user-id"}
        mock_keycloak.get_group.return_value = {
            "id": self.fixture.keycloak_group.backend_id,
        }

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.delete(self.delete_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_regular_user_cannot_delete_membership(self):
        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=regular_user)
        response = self.client.delete(self.delete_url)
        # Regular users can't see the membership (filtered by offering visibility)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class OfferingKeycloakMembershipFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()
        self.url = factories.OfferingKeycloakMembershipFactory.get_list_url()

    def test_filter_by_state(self):
        # The fixture membership is pending by default
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, {"state": "pending"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        # Filter by active should return none (fixture membership is pending)
        response = self.client.get(self.url, {"state": "active"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_filter_by_offering_uuid(self):
        self.client.force_authenticate(user=self.fixture.staff)

        # Filter with fixture offering UUID should return 1 membership
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        # Filter with a different offering UUID should return 0
        other_offering = marketplace_factories.OfferingFactory()
        response = self.client.get(self.url, {"offering_uuid": other_offering.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class BaseRemoteActionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.KeycloakFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_RESOURCE_USERS)


class TestConnectionTest(BaseRemoteActionTest):
    def setUp(self):
        super().setUp()
        self.url = factories.OfferingKeycloakGroupFactory.get_list_url(
            "test_connection"
        )

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_staff_can_test_connection(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.list_groups.return_value = [
            {"id": "g1", "name": "group1"},
            {"id": "g2", "name": "group2"},
        ]

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ok")
        self.assertEqual(response.data["groups_count"], 2)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_connection_failure_returns_400(self, mock_get_client):
        from keycloak import exceptions as keycloak_exceptions

        mock_get_client.side_effect = keycloak_exceptions.KeycloakConnectionError(
            "Connection refused"
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["status"], "error")

    def test_non_keycloak_offering_returns_400(self):
        offering = marketplace_factories.OfferingFactory()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.url, {"offering_uuid": offering.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_offering_uuid_returns_400(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_customer_owner_can_test_connection(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.list_groups.return_value = []

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_regular_user_cannot_test_connection(self, mock_get_client):
        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=regular_user)
        response = self.client.post(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RemoteGroupsTest(BaseRemoteActionTest):
    def setUp(self):
        super().setUp()
        self.url = factories.OfferingKeycloakGroupFactory.get_list_url("remote_groups")

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_lists_groups_filtered_by_offering_prefix(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        prefix = self.fixture.offering.uuid.hex
        mock_keycloak.list_groups.return_value = [
            {
                "id": "g1",
                "name": f"{prefix}_Member",
                "path": f"/{prefix}_Member",
                "subGroups": [],
            },
            {
                "id": "g2",
                "name": "unrelated_group",
                "path": "/unrelated_group",
                "subGroups": [],
            },
        ]

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], f"{prefix}_Member")
        self.assertEqual(response.data[0]["sub_group_count"], 0)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_regular_user_cannot_list_remote_groups(self, mock_get_client):
        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=regular_user)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_missing_offering_uuid_returns_400(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RemoteGroupMembersTest(BaseRemoteActionTest):
    def setUp(self):
        super().setUp()
        self.url = factories.OfferingKeycloakGroupFactory.get_list_url(
            "remote_group_members"
        )

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_lists_group_members(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.list_group_members.return_value = [
            {
                "id": "u1",
                "username": "alice",
                "email": "alice@example.com",
                "firstName": "Alice",
                "lastName": "Smith",
            },
        ]

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url,
            {
                "offering_uuid": self.fixture.offering.uuid.hex,
                "group_id": "g1",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["username"], "alice")
        self.assertEqual(response.data[0]["first_name"], "Alice")

    def test_missing_group_id_returns_400(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_invalid_group_id_returns_400(self, mock_get_client):
        from keycloak import exceptions as keycloak_exceptions

        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.list_group_members.side_effect = (
            keycloak_exceptions.KeycloakGetError("Group not found")
        )

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url,
            {
                "offering_uuid": self.fixture.offering.uuid.hex,
                "group_id": "nonexistent",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_regular_user_cannot_list_members(self, mock_get_client):
        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=regular_user)
        response = self.client.get(
            self.url,
            {
                "offering_uuid": self.fixture.offering.uuid.hex,
                "group_id": "g1",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SearchRemoteUsersTest(BaseRemoteActionTest):
    def setUp(self):
        super().setUp()
        self.url = factories.OfferingKeycloakGroupFactory.get_list_url(
            "search_remote_users"
        )

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_search_returns_matching_users(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.search_users.return_value = [
            {
                "id": "u1",
                "username": "bob",
                "email": "bob@example.com",
                "firstName": "Bob",
                "lastName": "Jones",
            },
        ]

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url,
            {
                "offering_uuid": self.fixture.offering.uuid.hex,
                "q": "bob",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["username"], "bob")

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_search_returns_empty_results(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.search_users.return_value = []

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url,
            {
                "offering_uuid": self.fixture.offering.uuid.hex,
                "q": "nonexistent",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_missing_query_returns_400(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_regular_user_cannot_search_users(self, mock_get_client):
        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=regular_user)
        response = self.client.get(
            self.url,
            {
                "offering_uuid": self.fixture.offering.uuid.hex,
                "q": "bob",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SyncStatusTest(BaseRemoteActionTest):
    def setUp(self):
        super().setUp()
        self.url = factories.OfferingKeycloakGroupFactory.get_list_url("sync_status")

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_detects_synced_groups(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        prefix = self.fixture.offering.uuid.hex
        backend_id = self.fixture.keycloak_group.backend_id
        group_name = f"{prefix}_Member"

        mock_keycloak.list_groups.return_value = [
            {"id": backend_id, "name": group_name, "path": f"/{group_name}"},
        ]

        # Update the local group name to match for clarity
        self.fixture.keycloak_group.name = group_name
        self.fixture.keycloak_group.save()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["synced"]), 1)
        self.assertEqual(response.data["synced"][0]["backend_id"], backend_id)
        self.assertEqual(len(response.data["local_only"]), 0)
        self.assertEqual(len(response.data["remote_only"]), 0)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_detects_remote_only_groups(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        prefix = self.fixture.offering.uuid.hex
        remote_name = f"{prefix}_Admin"

        mock_keycloak.list_groups.return_value = [
            {"id": "remote-only-id", "name": remote_name, "path": f"/{remote_name}"},
        ]

        # Ensure local group's backend_id doesn't match any remote group
        self.fixture.keycloak_group.backend_id = "local-backend-id"
        self.fixture.keycloak_group.save()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(remote_name, response.data["remote_only"])

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_detects_local_only_groups(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak

        # No remote groups matching the offering prefix
        mock_keycloak.list_groups.return_value = []

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["local_only"]), 1)
        self.assertEqual(len(response.data["remote_only"]), 0)

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_detects_local_groups_without_backend_id(self, mock_get_client):
        mock_keycloak = mock.MagicMock()
        mock_get_client.return_value = mock_keycloak
        mock_keycloak.list_groups.return_value = []

        # Create a local group without backend_id
        self.fixture.keycloak_group.backend_id = ""
        self.fixture.keycloak_group.save()

        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.fixture.keycloak_group.name, response.data["local_only"])

    @mock.patch("waldur_keycloak.views.utils.get_keycloak_client_for_offering")
    def test_regular_user_cannot_view_sync_status(self, mock_get_client):
        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=regular_user)
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
