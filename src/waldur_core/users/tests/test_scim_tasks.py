from datetime import timedelta
from unittest import mock

from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.scim import tasks
from waldur_core.users.scim.client import ScimClient, ScimError
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


@override_config(
    SCIM_MEMBERSHIP_SYNC_ENABLED=True,
    SCIM_API_URL="https://scim.example.org",
    SCIM_API_KEY="secret",
    SCIM_URN_NAMESPACE="urn:ietf:dev",
)
class ScimTasksTest(TestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.user = structure_factories.UserFactory(
            username="11111111-1111-1111-1111-111111111111@myaccessid.org"
        )
        self.urn_namespace = "urn:ietf:dev"
        self.ssh_username = self.user.username

    def _create_offering_with_ssh_endpoint(self, login_node="login.example.org"):
        """Create an offering with SSH endpoint."""
        offering = marketplace_factories.OfferingFactory()
        marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=offering,
            name="SSH Access",
            url=f"ssh://{login_node}",
        )
        return offering

    def _create_resource_with_ssh_endpoint(
        self, project, login_node="login.example.org"
    ):
        """Create a resource in project with SSH endpoint."""
        offering = self._create_offering_with_ssh_endpoint(login_node)
        resource = marketplace_factories.ResourceFactory(
            project=project,
            offering=offering,
            state=marketplace_models.Resource.States.OK,
        )
        return resource, offering

    def _grant_project_role(self, user=None, project=None):
        """Grant project role to user."""
        if user is None:
            user = self.user
        if project is None:
            project = self.project
        project_ct = ContentType.objects.get_for_model(structure_models.Project)
        role = Role.objects.get_system_role("Project member", project_ct)
        return UserRole.objects.create(
            user=user,
            role=role,
            scope=project,
            is_active=True,
        )

    def _mock_client(self):
        """Create a mock SCIM client."""
        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        return client

    def test_add_entitlements_when_user_has_project_role_and_resource(self):
        """Test adding entitlements when user has project role and resource with SSH endpoint."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        expected_entitlement = client.build_entitlement(
            self.urn_namespace, "login.example.org", self.ssh_username
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.add_entitlements.assert_called_once_with(
            self.user.username, [expected_entitlement]
        )
        client.remove_entitlements.assert_not_called()
        client.clear_all_entitlements.assert_not_called()

    def test_add_multiple_entitlements_for_multiple_login_nodes(self):
        """Test adding multiple entitlements when user has resources with different SSH endpoints."""
        self._grant_project_role()
        resource1, offering1 = self._create_resource_with_ssh_endpoint(
            self.project, "login1.example.org"
        )
        resource2, offering2 = self._create_resource_with_ssh_endpoint(
            self.project, "login2.example.org"
        )
        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        expected_entitlements = [
            client.build_entitlement(
                self.urn_namespace, "login1.example.org", self.ssh_username
            ),
            client.build_entitlement(
                self.urn_namespace, "login2.example.org", self.ssh_username
            ),
        ]

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        call_args = client.add_entitlements.call_args
        self.assertEqual(call_args[0][0], self.user.username)
        self.assertEqual(set(call_args[0][1]), set(expected_entitlements))
        self.assertEqual(len(call_args[0][1]), 2)

    def test_remove_entitlements_when_user_has_no_project_roles(self):
        """Test removing entitlements when user has no project roles."""
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        client = self._mock_client()
        entitlement = client.build_entitlement(
            self.urn_namespace, "login.example.org", self.ssh_username
        )
        client.get_user.return_value = {"entitlements": [{"value": entitlement}]}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.clear_all_entitlements.assert_called_once_with(self.user.username)
        client.add_entitlements.assert_not_called()
        client.remove_entitlements.assert_not_called()

    def test_remove_stale_entitlements_when_login_node_changes(self):
        """Test removing stale entitlements when user's resources change."""
        self._grant_project_role()
        # User had access to login1, now only has login2
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login2.example.org"
        )
        client = self._mock_client()
        old_entitlement = client.build_entitlement(
            self.urn_namespace, "login1.example.org", self.ssh_username
        )
        new_entitlement = client.build_entitlement(
            self.urn_namespace, "login2.example.org", self.ssh_username
        )
        client.get_user.return_value = {"entitlements": [{"value": old_entitlement}]}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.add_entitlements.assert_called_once_with(
            self.user.username, [new_entitlement]
        )
        client.remove_entitlements.assert_called_once_with(
            self.user.username, [old_entitlement]
        )

    def test_clear_all_entitlements_when_user_inactive(self):
        """Test clearing all entitlements when user is inactive."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        client = self._mock_client()
        entitlement = client.build_entitlement(
            self.urn_namespace, "login.example.org", self.ssh_username
        )
        client.get_user.return_value = {"entitlements": [{"value": entitlement}]}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.clear_all_entitlements.assert_called_once_with(self.user.username)
        client.add_entitlements.assert_not_called()
        client.remove_entitlements.assert_not_called()

    def test_skip_when_user_has_no_username(self):
        """Test skipping sync when user has no username."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        self.user.username = ""
        self.user.save(update_fields=["username"])

        client = self._mock_client()

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.get_user.assert_not_called()
        client.add_entitlements.assert_not_called()

    def test_skip_when_no_ssh_endpoints(self):
        """Test skipping sync when user has resources but no SSH endpoints."""
        self._grant_project_role()
        offering = marketplace_factories.OfferingFactory()
        marketplace_factories.ResourceFactory(
            project=self.project,
            offering=offering,
            state=marketplace_models.Resource.States.OK,
        )

        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.get_user.assert_called_once_with(self.user.username)
        client.add_entitlements.assert_not_called()
        client.clear_all_entitlements.assert_not_called()

    def test_skip_when_no_resources(self):
        """Test skipping sync when user has project role but no resources."""
        self._grant_project_role()

        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.get_user.assert_called_once_with(self.user.username)
        client.add_entitlements.assert_not_called()
        client.clear_all_entitlements.assert_not_called()

    def test_skip_when_resource_not_ok_state(self):
        """Test skipping sync when resource is not in OK state."""
        self._grant_project_role()
        offering = self._create_offering_with_ssh_endpoint("login.example.org")
        marketplace_factories.ResourceFactory(
            project=self.project,
            offering=offering,
            state=marketplace_models.Resource.States.CREATING,
        )

        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.get_user.assert_called_once_with(self.user.username)
        client.clear_all_entitlements.assert_not_called()

    def test_handle_scim_error_on_get_user(self):
        """Test handling SCIM error when getting user."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        client = self._mock_client()
        client.get_user.side_effect = ScimError("User not found")

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.add_entitlements.assert_not_called()
        client.remove_entitlements.assert_not_called()

    def test_handle_scim_error_on_update(self):
        """Test handling SCIM error when updating entitlements."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}
        client.add_entitlements.side_effect = ScimError("Update failed")

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client.add_entitlements.assert_called_once()

    def test_sync_user_entitlements_task_uses_user_uuid(self):
        """Test that sync_user_entitlements task uses user UUID."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        with mock.patch(
            "waldur_core.users.scim.tasks.ScimClient", return_value=client
        ) as client_factory:
            tasks.sync_user_entitlements(self.user.uuid.hex)

        client_factory.assert_called_once()
        client.get_user.assert_called_once_with(self.user.username)

    def test_skip_when_settings_missing_required_options(self):
        """Test skipping sync when required SCIM settings are missing."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        client = self._mock_client()

        with override_config(SCIM_URN_NAMESPACE=""):
            with mock.patch(
                "waldur_core.users.scim.tasks.ScimClient", return_value=client
            ):
                tasks.sync_user_entitlements(self.user.uuid.hex)

        client.get_user.assert_not_called()
        client.add_entitlements.assert_not_called()

    def test_skip_when_scim_disabled(self):
        """Test skipping sync when SCIM is disabled."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        client = self._mock_client()

        with override_config(SCIM_MEMBERSHIP_SYNC_ENABLED=False):
            with mock.patch(
                "waldur_core.users.scim.tasks.ScimClient", return_value=client
            ):
                tasks.sync_user_entitlements(self.user.uuid.hex)

        client.get_user.assert_not_called()

    def test_extract_hostname_from_ssh_url(self):
        """Test extracting hostname from SSH URL."""
        self.assertEqual(
            tasks.extract_hostname_from_ssh_url("ssh://login.example.org"),
            "login.example.org",
        )
        self.assertEqual(
            tasks.extract_hostname_from_ssh_url("ssh://login.example.org:22"),
            "login.example.org",
        )
        self.assertIsNone(tasks.extract_hostname_from_ssh_url("http://example.org"))
        self.assertIsNone(tasks.extract_hostname_from_ssh_url("invalid"))

    def test_get_user_ssh_login_nodes(self):
        """Test getting SSH login nodes from user's resources."""
        self._grant_project_role()
        resource1, offering1 = self._create_resource_with_ssh_endpoint(
            self.project, "login1.example.org"
        )
        resource2, offering2 = self._create_resource_with_ssh_endpoint(
            self.project, "login2.example.org"
        )

        login_nodes = tasks.get_user_ssh_login_nodes(self.user)
        self.assertEqual(login_nodes, {"login1.example.org", "login2.example.org"})

    def test_get_user_ssh_login_nodes_empty_when_no_resources(self):
        """Test getting empty set when user has no resources."""
        self._grant_project_role()
        login_nodes = tasks.get_user_ssh_login_nodes(self.user)
        self.assertEqual(login_nodes, set())

    def test_get_user_ssh_login_nodes_empty_when_no_roles(self):
        """Test getting empty set when user has no project roles."""
        login_nodes = tasks.get_user_ssh_login_nodes(self.user)
        self.assertEqual(login_nodes, set())


@override_config(
    SCIM_MEMBERSHIP_SYNC_ENABLED=True,
    SCIM_API_URL="https://scim.example.org",
    SCIM_API_KEY="secret",
    SCIM_URN_NAMESPACE="urn:ietf:dev",
)
class ScimReconcileTasksTest(TestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.user = structure_factories.UserFactory(
            username="11111111-1111-1111-1111-111111111111@myaccessid.org"
        )

    def _create_resource_with_ssh_endpoint(
        self, project, login_node="login.example.org"
    ):
        """Create a resource in project with SSH endpoint."""
        offering = marketplace_factories.OfferingFactory()
        marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=offering,
            name="SSH Access",
            url=f"ssh://{login_node}",
        )
        return marketplace_factories.ResourceFactory(
            project=project,
            offering=offering,
            state=marketplace_models.Resource.States.OK,
        )

    def _grant_project_role(self, user=None, project=None, modified=None):
        """Grant project role to user with optional modified timestamp."""
        if user is None:
            user = self.user
        if project is None:
            project = self.project
        project_ct = ContentType.objects.get_for_model(structure_models.Project)
        role = Role.objects.get_system_role("Project member", project_ct)
        role_obj = UserRole.objects.create(
            user=user,
            role=role,
            scope=project,
            is_active=True,
        )
        if modified:
            UserRole.objects.filter(pk=role_obj.pk).update(modified=modified)
            role_obj.refresh_from_db()
        return role_obj

    def _create_batch_capture(self):
        batch_calls = []

        def capture_batch_call(*args, **kwargs):
            batch_calls.append(args[0])
            return tasks.sync_user_batch_entitlements(*args, **kwargs)

        return batch_calls, capture_batch_call

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_recent_entitlements_includes_recently_modified_roles(
        self, mock_batch_delay
    ):
        """Test that sync_recent_entitlements includes users with recently modified roles."""
        self._create_resource_with_ssh_endpoint(self.project, "login.example.org")
        self._grant_project_role(modified=timezone.now() - timedelta(hours=1))

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}
        mock_batch_delay.side_effect = (
            lambda *args, **kwargs: tasks.sync_user_batch_entitlements(*args, **kwargs)
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.get_user.assert_called_once_with(self.user.username)

    def test_sync_recent_entitlements_excludes_old_roles(self):
        """Test that sync_recent_entitlements excludes users with old role modifications."""
        self._create_resource_with_ssh_endpoint(self.project, "login.example.org")
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.get_user.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_all_entitlements_includes_all_users_with_roles(
        self, mock_batch_delay
    ):
        """Test that sync_all_entitlements includes all users with active roles."""
        self._create_resource_with_ssh_endpoint(self.project, "login.example.org")
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}
        # Let the mock execute the batch tasks synchronously
        mock_batch_delay.side_effect = (
            lambda *args, **kwargs: tasks.sync_user_batch_entitlements(*args, **kwargs)
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_all_entitlements()

        client.get_user.assert_called_once_with(self.user.username)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_recent_entitlements_batches_users_correctly(self, mock_batch_delay):
        """Test that sync_recent_entitlements splits users into batches of correct size."""
        # Create 22 users with roles (batch size is 20, so should create 2 batches: 20 + 2)
        self._create_resource_with_ssh_endpoint(self.project, "login.example.org")
        users = []
        recent_time = timezone.now() - timedelta(hours=1)

        for i in range(22):
            user = structure_factories.UserFactory(username=f"user-{i}@example.org")
            users.append(user)
            self._grant_project_role(user=user, modified=recent_time)

        batch_calls, capture_batch_call = self._create_batch_capture()
        mock_batch_delay.side_effect = capture_batch_call

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        self.assertEqual(mock_batch_delay.call_count, 2)

        first_batch = batch_calls[0]
        second_batch = batch_calls[1]
        self.assertEqual(len(first_batch), 20, "First batch should contain 20 users")
        self.assertEqual(len(second_batch), 2, "Second batch should contain 2 users")

        all_batched_uuids = set(first_batch + second_batch)
        all_user_uuids = {str(user.uuid) for user in users}
        self.assertEqual(
            all_batched_uuids, all_user_uuids, "All users should be included in batches"
        )

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_all_entitlements_batches_users_correctly(self, mock_batch_delay):
        """Test that sync_all_entitlements splits users into batches of correct size."""
        # Create 22 users with roles (batch size is 20, so should create 2 batches: 20 + 2)
        self._create_resource_with_ssh_endpoint(self.project, "login.example.org")
        users = []

        for i in range(22):
            user = structure_factories.UserFactory(username=f"user-all-{i}@example.org")
            users.append(user)
            self._grant_project_role(user=user)

        batch_calls, capture_batch_call = self._create_batch_capture()
        mock_batch_delay.side_effect = capture_batch_call

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_all_entitlements()

        self.assertEqual(mock_batch_delay.call_count, 2)

        first_batch = batch_calls[0]
        second_batch = batch_calls[1]
        self.assertEqual(len(first_batch), 20, "First batch should contain 20 users")
        self.assertEqual(len(second_batch), 2, "Second batch should contain 2 users")

        all_batched_uuids = set(first_batch + second_batch)
        all_user_uuids = {user.uuid.hex for user in users}
        self.assertEqual(
            all_batched_uuids, all_user_uuids, "All users should be included in batches"
        )


class ScimSyncAllApiTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory(is_staff=False)
        self.url = structure_factories.UserFactory.get_list_url("scim_sync_all")

    def test_staff_can_trigger_scim_sync(self):
        """Test that staff users can trigger SCIM sync."""
        self.client.force_authenticate(self.staff_user)
        with mock.patch.object(tasks.sync_all_entitlements, "delay") as mock_delay:
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_delay.assert_called_once_with()

    def test_regular_user_cannot_trigger_scim_sync(self):
        """Test that regular users cannot trigger SCIM sync."""
        self.client.force_authenticate(self.regular_user)
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
