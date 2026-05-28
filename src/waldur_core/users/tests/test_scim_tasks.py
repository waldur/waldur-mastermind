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
from waldur_mastermind.marketplace.enums import OfferingUserStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class BaseScimTestCase(TestCase):
    """Base class for SCIM tests with shared helper methods."""

    def _create_offering_with_ssh_endpoint(self, login_node="login.example.org"):
        """Create an offering with SSH endpoint."""
        offering = marketplace_factories.OfferingFactory()
        marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=offering,
            name="SSH Access",
            url=f"ssh://{login_node}",
        )
        return offering

    def _create_offering_user(
        self, user=None, offering=None, username=None, state=OfferingUserStates.OK
    ):
        """Create an offering user with a specific username."""
        if user is None:
            raise ValueError("user parameter is required")
        if username is None:
            username = f"{user.username}-on-offering"
        return marketplace_models.OfferingUser.objects.create(
            user=user,
            offering=offering,
            username=username,
            state=state,
        )

    def _grant_project_role(self, user, project, modified=None):
        """Grant project member role to user. Optionally override the modified timestamp."""
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


@override_config(
    SCIM_MEMBERSHIP_SYNC_ENABLED=True,
    SCIM_API_URL="https://scim.example.org",
    SCIM_API_KEY="secret",
    SCIM_URN_NAMESPACE="urn:ietf:dev",
)
class ScimTasksTest(BaseScimTestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.user = structure_factories.UserFactory(
            username="11111111-1111-1111-1111-111111111111@myaccessid.org"
        )
        self.urn_namespace = "urn:ietf:dev"
        self.ssh_username = self.user.username

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
        return super()._grant_project_role(
            user=user or self.user,
            project=project or self.project,
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
        offering_username = "user-on-offering"
        self._create_offering_user(
            user=self.user, offering=offering, username=offering_username
        )

        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        expected_entitlement = client.build_entitlement(
            self.urn_namespace, "login.example.org", offering_username
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

        # Create offering users with different usernames for each offering
        offering1_username = "user-on-offering1"
        offering2_username = "user-on-offering2"
        self._create_offering_user(
            user=self.user, offering=offering1, username=offering1_username
        )
        self._create_offering_user(
            user=self.user, offering=offering2, username=offering2_username
        )

        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        expected_entitlements = [
            client.build_entitlement(
                self.urn_namespace, "login1.example.org", offering1_username
            ),
            client.build_entitlement(
                self.urn_namespace, "login2.example.org", offering2_username
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
        offering_username = "user-on-offering"
        self._create_offering_user(
            user=self.user, offering=offering, username=offering_username
        )

        client = self._mock_client()
        entitlement = client.build_entitlement(
            self.urn_namespace, "login.example.org", offering_username
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
        offering_username = "user-on-offering"
        self._create_offering_user(
            user=self.user, offering=offering, username=offering_username
        )

        client = self._mock_client()
        old_entitlement = client.build_entitlement(
            self.urn_namespace, "login1.example.org", offering_username
        )
        new_entitlement = client.build_entitlement(
            self.urn_namespace, "login2.example.org", offering_username
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
        offering_username = "user-on-offering"
        self._create_offering_user(
            user=self.user, offering=offering, username=offering_username
        )
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        client = self._mock_client()
        entitlement = client.build_entitlement(
            self.urn_namespace, "login.example.org", offering_username
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
        # Create offering user but no SSH endpoints
        self._create_offering_user(user=self.user, offering=offering)

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
        self._create_offering_user(user=self.user, offering=offering)

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
        self._create_offering_user(user=self.user, offering=offering)

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
        self._create_offering_user(user=self.user, offering=offering)

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
        self._create_offering_user(user=self.user, offering=offering)

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
        self._create_offering_user(user=self.user, offering=offering)

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
        offering1_username = "user-on-offering1"
        offering2_username = "user-on-offering2"
        self._create_offering_user(
            user=self.user, offering=offering1, username=offering1_username
        )
        self._create_offering_user(
            user=self.user, offering=offering2, username=offering2_username
        )

        login_nodes = tasks.get_user_ssh_login_nodes(self.user)
        self.assertEqual(
            login_nodes,
            {
                "login1.example.org": offering1_username,
                "login2.example.org": offering2_username,
            },
        )

    def test_get_user_ssh_login_nodes_empty_when_no_resources(self):
        """Test getting empty dict when user has no resources."""
        self._grant_project_role()
        login_nodes = tasks.get_user_ssh_login_nodes(self.user)
        self.assertEqual(login_nodes, {})

    def test_get_user_ssh_login_nodes_empty_when_no_roles(self):
        """Test getting empty dict when user has no project roles."""
        login_nodes = tasks.get_user_ssh_login_nodes(self.user)
        self.assertEqual(login_nodes, {})

    def test_skip_offering_user_not_in_ok_state(self):
        """Test that offering users not in OK state are excluded from entitlements."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )

        marketplace_models.OfferingUser.objects.filter(
            user=self.user, offering=offering
        ).delete()

        self._create_offering_user(
            user=self.user,
            offering=offering,
            username="",
            state=OfferingUserStates.CREATING,
        )

        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        # Should not add entitlements since offering user is not in OK state
        client.get_user.assert_called_once_with(self.user.username)
        client.add_entitlements.assert_not_called()
        client.clear_all_entitlements.assert_not_called()

    def test_skip_offering_user_without_username(self):
        """Test that offering users without username are excluded from entitlements."""
        self._grant_project_role()
        resource, offering = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        self._create_offering_user(
            user=self.user,
            offering=offering,
            username="",
            state=OfferingUserStates.OK,
        )

        client = self._mock_client()
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_user_entitlements(self.user.uuid.hex)

        # Should not add entitlements since offering user has no username
        client.get_user.assert_called_once_with(self.user.username)
        client.add_entitlements.assert_not_called()
        client.clear_all_entitlements.assert_not_called()


@override_config(
    SCIM_MEMBERSHIP_SYNC_ENABLED=True,
    SCIM_API_URL="https://scim.example.org",
    SCIM_API_KEY="secret",
    SCIM_URN_NAMESPACE="urn:ietf:dev",
)
class ScimReconcileTasksTest(BaseScimTestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.user = structure_factories.UserFactory(
            username="11111111-1111-1111-1111-111111111111@myaccessid.org"
        )

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
        return resource

    def _grant_project_role(self, user=None, project=None, modified=None):
        return super()._grant_project_role(
            user=user or self.user,
            project=project or self.project,
            modified=modified,
        )

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
        resource = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        self._create_offering_user(user=self.user, offering=resource.offering)
        self._grant_project_role(modified=timezone.now() - timedelta(hours=1))

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}
        mock_batch_delay.side_effect = lambda *args, **kwargs: (
            tasks.sync_user_batch_entitlements(*args, **kwargs)
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
    def test_sync_recent_entitlements_includes_recent_offering_user_change(
        self, mock_batch_delay
    ):
        """Reconcile users when OfferingUser became ready but project role is unchanged."""
        resource = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        offering_user = self._create_offering_user(
            user=self.user, offering=resource.offering
        )
        marketplace_models.OfferingUser.objects.filter(pk=offering_user.pk).update(
            modified=timezone.now() - timedelta(hours=1)
        )

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}
        mock_batch_delay.side_effect = lambda *args, **kwargs: (
            tasks.sync_user_batch_entitlements(*args, **kwargs)
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.get_user.assert_called_once_with(self.user.username)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_recent_entitlements_includes_recent_resource_change(
        self, mock_batch_delay
    ):
        """Reconcile offering users when a resource became OK but project role is unchanged."""
        offering = self._create_offering_with_ssh_endpoint("login.example.org")
        resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=offering,
            state=ResourceStates.OK,
        )
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        self._create_offering_user(user=self.user, offering=offering)
        marketplace_models.Resource.objects.filter(pk=resource.pk).update(
            modified=timezone.now() - timedelta(hours=1)
        )

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}
        mock_batch_delay.side_effect = lambda *args, **kwargs: (
            tasks.sync_user_batch_entitlements(*args, **kwargs)
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.get_user.assert_called_once_with(self.user.username)

    def test_sync_recent_entitlements_excludes_stale_marketplace_changes(self):
        """Old OfferingUser/Resource changes do not trigger reconcile without role updates."""
        offering = self._create_offering_with_ssh_endpoint("login.example.org")
        resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=offering,
            state=ResourceStates.OK,
        )
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        offering_user = self._create_offering_user(user=self.user, offering=offering)
        stale_time = timezone.now() - timedelta(hours=3)
        marketplace_models.OfferingUser.objects.filter(pk=offering_user.pk).update(
            modified=stale_time
        )
        marketplace_models.Resource.objects.filter(pk=resource.pk).update(
            modified=stale_time
        )

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.get_user.assert_not_called()

    def test_get_users_for_reconciliation_unions_role_and_marketplace_sources(self):
        """Unit check that marketplace changes expand the reconcile user set."""
        offering = self._create_offering_with_ssh_endpoint("login.example.org")
        resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=offering,
            state=ResourceStates.OK,
        )
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        offering_user = self._create_offering_user(user=self.user, offering=offering)
        marketplace_models.OfferingUser.objects.filter(pk=offering_user.pk).update(
            modified=timezone.now() - timedelta(hours=1)
        )
        marketplace_models.Resource.objects.filter(pk=resource.pk).update(
            modified=timezone.now() - timedelta(hours=3)
        )

        users = tasks.get_users_for_reconciliation()

        self.assertEqual(users.count(), 1)
        self.assertEqual(users.get(), self.user)

    def test_get_users_for_reconciliation_includes_recently_revoked_role(self):
        """Revoked roles are reconcile candidates so stale entitlements can be cleared."""
        role = self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        role.revoke()

        users = tasks.get_users_for_reconciliation()

        self.assertEqual(users.count(), 1)
        self.assertEqual(users.get(), self.user)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_recent_entitlements_clears_entitlements_after_recent_role_revocation(
        self, mock_batch_delay
    ):
        """Reconcile runs cleanup when a revocation event was missed."""
        resource = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        offering_username = "user-on-offering"
        self._create_offering_user(
            user=self.user,
            offering=resource.offering,
            username=offering_username,
        )
        role = self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        role.revoke()

        entitlement = ScimClient.build_entitlement(
            "urn:ietf:dev", "login.example.org", offering_username
        )
        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": [{"value": entitlement}]}
        mock_batch_delay.side_effect = lambda *args, **kwargs: (
            tasks.sync_user_batch_entitlements(*args, **kwargs)
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.clear_all_entitlements.assert_called_once_with(self.user.username)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_recent_entitlements_includes_recent_offering_user_deactivation(
        self, mock_batch_delay
    ):
        """Reconcile users when OfferingUser left OK but project role is unchanged."""
        resource = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        offering_username = "user-on-offering"
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        offering_user = self._create_offering_user(
            user=self.user,
            offering=resource.offering,
            username=offering_username,
        )
        offering_user.state = OfferingUserStates.DELETED
        offering_user.save()
        marketplace_models.OfferingUser.objects.filter(pk=offering_user.pk).update(
            modified=timezone.now() - timedelta(hours=1)
        )

        entitlement = ScimClient.build_entitlement(
            "urn:ietf:dev", "login.example.org", offering_username
        )
        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": [{"value": entitlement}]}
        mock_batch_delay.side_effect = lambda *args, **kwargs: (
            tasks.sync_user_batch_entitlements(*args, **kwargs)
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.clear_all_entitlements.assert_called_once_with(self.user.username)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_recent_entitlements_includes_recent_resource_termination(
        self, mock_batch_delay
    ):
        """Reconcile offering users when a resource left OK but project role is unchanged."""
        offering = self._create_offering_with_ssh_endpoint("login.example.org")
        offering_username = "user-on-offering"
        resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=offering,
            state=ResourceStates.OK,
        )
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        self._create_offering_user(
            user=self.user, offering=offering, username=offering_username
        )
        resource.set_state_terminated()
        resource.save()
        marketplace_models.Resource.objects.filter(pk=resource.pk).update(
            modified=timezone.now() - timedelta(hours=1)
        )

        entitlement = ScimClient.build_entitlement(
            "urn:ietf:dev", "login.example.org", offering_username
        )
        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": [{"value": entitlement}]}
        mock_batch_delay.side_effect = lambda *args, **kwargs: (
            tasks.sync_user_batch_entitlements(*args, **kwargs)
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.clear_all_entitlements.assert_called_once_with(self.user.username)

    def test_sync_recent_entitlements_excludes_stale_role_revocation(self):
        """Old revocations outside the lookback window are not reconciled."""
        resource = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        self._create_offering_user(user=self.user, offering=resource.offering)
        role = self._grant_project_role(modified=timezone.now() - timedelta(hours=3))
        role.revoke()
        UserRole.objects.filter(pk=role.pk).update(
            modified=timezone.now() - timedelta(hours=3)
        )

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": [{"value": "stale"}]}

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_recent_entitlements()

        client.get_user.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_all_entitlements_includes_all_users_with_roles(
        self, mock_batch_delay
    ):
        """Test that sync_all_entitlements includes all users with active roles."""
        resource = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        self._create_offering_user(user=self.user, offering=resource.offering)
        self._grant_project_role(modified=timezone.now() - timedelta(hours=3))

        client = mock.Mock(spec=ScimClient)
        client.build_entitlement = ScimClient.build_entitlement
        client.get_user.return_value = {"entitlements": []}
        # Let the mock execute the batch tasks synchronously
        mock_batch_delay.side_effect = lambda *args, **kwargs: (
            tasks.sync_user_batch_entitlements(*args, **kwargs)
        )

        with mock.patch("waldur_core.users.scim.tasks.ScimClient", return_value=client):
            tasks.sync_all_entitlements()

        client.get_user.assert_called_once_with(self.user.username)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_recent_entitlements_batches_users_correctly(self, mock_batch_delay):
        """Test that sync_recent_entitlements splits users into batches of correct size."""
        # Create 22 users with roles (batch size is 20, so should create 2 batches: 20 + 2)
        resource = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        users = []
        recent_time = timezone.now() - timedelta(hours=1)

        for i in range(22):
            user = structure_factories.UserFactory(username=f"user-{i}@example.org")
            users.append(user)
            self._create_offering_user(user=user, offering=resource.offering)
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
        all_user_uuids = {user.uuid.hex for user in users}
        self.assertEqual(
            all_batched_uuids, all_user_uuids, "All users should be included in batches"
        )

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_sync_all_entitlements_batches_users_correctly(self, mock_batch_delay):
        """Test that sync_all_entitlements splits users into batches of correct size."""
        # Create 22 users with roles (batch size is 20, so should create 2 batches: 20 + 2)
        resource = self._create_resource_with_ssh_endpoint(
            self.project, "login.example.org"
        )
        users = []

        for i in range(22):
            user = structure_factories.UserFactory(username=f"user-all-{i}@example.org")
            users.append(user)
            self._create_offering_user(user=user, offering=resource.offering)
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


class ScimSyncAllApiTest(test.APITestCase):
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


@override_config(
    SCIM_MEMBERSHIP_SYNC_ENABLED=True,
    SCIM_API_URL="https://scim.example.org",
    SCIM_API_KEY="secret",
    SCIM_URN_NAMESPACE="urn:ietf:dev",
)
class ScimEndpointChangeTest(BaseScimTestCase):
    """Test that the signal handler dispatches the right task — nothing more."""

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()

    @mock.patch("waldur_core.users.scim.tasks.sync_users_for_offering_endpoint.delay")
    def test_ssh_endpoint_creation_triggers_scim_sync(self, mock_task_delay):
        """Creating an SSH endpoint dispatches sync for that offering."""
        marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=self.offering,
            name="SSH Access",
            url="ssh://login.example.org",
        )

        mock_task_delay.assert_called_once_with(self.offering.uuid.hex)

    @mock.patch("waldur_core.users.scim.tasks.sync_users_for_offering_endpoint.delay")
    def test_ssh_endpoint_update_triggers_scim_sync(self, mock_task_delay):
        """Updating an SSH endpoint dispatches sync for that offering."""
        endpoint = marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=self.offering,
            name="SSH Access",
            url="ssh://login.example.org",
        )
        mock_task_delay.reset_mock()

        endpoint.url = "ssh://new-login.example.org"
        endpoint.save()

        mock_task_delay.assert_called_once_with(self.offering.uuid.hex)

    @mock.patch("waldur_core.users.scim.tasks.sync_users_for_offering_endpoint.delay")
    def test_ssh_endpoint_deletion_triggers_scim_sync(self, mock_task_delay):
        """Deleting an SSH endpoint dispatches sync for that offering."""
        endpoint = marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=self.offering,
            name="SSH Access",
            url="ssh://login.example.org",
        )
        mock_task_delay.reset_mock()

        endpoint.delete()

        mock_task_delay.assert_called_once_with(self.offering.uuid.hex)

    @mock.patch("waldur_core.users.scim.tasks.sync_users_for_offering_endpoint.delay")
    def test_non_ssh_endpoint_does_not_trigger_sync(self, mock_task_delay):
        """Non-SSH endpoints must not trigger a sync."""
        marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=self.offering,
            name="HTTP Access",
            url="https://example.org",
        )

        mock_task_delay.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_users_for_offering_endpoint.delay")
    def test_endpoint_change_skips_when_scim_disabled(self, mock_task_delay):
        """No task is dispatched when SCIM sync is disabled."""
        with override_config(SCIM_MEMBERSHIP_SYNC_ENABLED=False):
            marketplace_models.OfferingAccessEndpoint.objects.create(
                offering=self.offering,
                name="SSH Access",
                url="ssh://login.example.org",
            )

        mock_task_delay.assert_not_called()


@override_config(
    SCIM_MEMBERSHIP_SYNC_ENABLED=True,
    SCIM_API_URL="https://scim.example.org",
    SCIM_API_KEY="secret",
    SCIM_URN_NAMESPACE="urn:ietf:dev",
)
class ScimSyncUsersForOfferingTaskTest(BaseScimTestCase):
    """Test sync_users_for_offering_endpoint: user discovery, filtering and batching."""

    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()
        self.user1 = structure_factories.UserFactory(username="user1@example.org")
        self.user2 = structure_factories.UserFactory(username="user2@example.org")
        self._create_offering_user(
            user=self.user1, offering=self.offering, username="user1-on-offering"
        )
        self._create_offering_user(
            user=self.user2, offering=self.offering, username="user2-on-offering"
        )

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_syncs_eligible_users(self, mock_batch_delay):
        """Task dispatches a batch containing all eligible offering users."""
        tasks.sync_users_for_offering_endpoint(self.offering.uuid.hex)

        mock_batch_delay.assert_called_once()
        batch = mock_batch_delay.call_args[0][0]
        self.assertEqual(set(batch), {self.user1.uuid.hex, self.user2.uuid.hex})

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_skips_users_without_offering_users(self, mock_batch_delay):
        """Users with no OfferingUser for this offering are not included."""
        user3 = structure_factories.UserFactory(username="user3@example.org")
        # user3 has no OfferingUser — they should not appear in the batch

        tasks.sync_users_for_offering_endpoint(self.offering.uuid.hex)

        batch = mock_batch_delay.call_args[0][0]
        self.assertNotIn(user3.uuid.hex, batch)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_skips_offering_users_not_in_ok_state(self, mock_batch_delay):
        """OfferingUsers not in OK state are excluded."""
        user3 = structure_factories.UserFactory(username="user3@example.org")
        offering_user3 = self._create_offering_user(
            user=user3, offering=self.offering, username="user3-on-offering"
        )
        marketplace_models.OfferingUser.objects.filter(pk=offering_user3.pk).update(
            state=OfferingUserStates.CREATING
        )

        tasks.sync_users_for_offering_endpoint(self.offering.uuid.hex)

        batch = mock_batch_delay.call_args[0][0]
        self.assertNotIn(user3.uuid.hex, batch)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_skips_offering_users_without_username(self, mock_batch_delay):
        """OfferingUsers with an empty username are excluded."""
        user3 = structure_factories.UserFactory(username="user3@example.org")
        marketplace_models.OfferingUser.objects.create(
            user=user3,
            offering=self.offering,
            username="",
            state=OfferingUserStates.OK,
        )

        tasks.sync_users_for_offering_endpoint(self.offering.uuid.hex)

        batch = mock_batch_delay.call_args[0][0]
        self.assertNotIn(user3.uuid.hex, batch)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_skips_when_no_eligible_users(self, mock_batch_delay):
        """No batch is dispatched when the offering has no eligible users."""
        marketplace_models.OfferingUser.objects.filter(offering=self.offering).delete()

        tasks.sync_users_for_offering_endpoint(self.offering.uuid.hex)

        mock_batch_delay.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_batch_entitlements.delay")
    def test_batches_users_correctly(self, mock_batch_delay):
        """Users are split into batches of DEFAULT_SCIM_BATCH_SIZE (20)."""
        # setUp has 2 users; add 22 more → 24 total → batches of 20 + 4.
        extra_users = []
        for i in range(22):
            user = structure_factories.UserFactory(username=f"user-{i}@example.org")
            extra_users.append(user)
            self._create_offering_user(
                user=user, offering=self.offering, username=f"user-{i}-on-offering"
            )

        tasks.sync_users_for_offering_endpoint(self.offering.uuid.hex)

        self.assertEqual(mock_batch_delay.call_count, 2)
        first_batch = mock_batch_delay.call_args_list[0][0][0]
        second_batch = mock_batch_delay.call_args_list[1][0][0]
        self.assertEqual(len(first_batch), 20)
        self.assertEqual(len(second_batch), 4)

        all_batched = set(first_batch + second_batch)
        expected = {self.user1.uuid.hex, self.user2.uuid.hex} | {
            u.uuid.hex for u in extra_users
        }
        self.assertEqual(all_batched, expected)


@override_config(
    SCIM_MEMBERSHIP_SYNC_ENABLED=True,
    SCIM_API_URL="https://scim.example.org",
    SCIM_API_KEY="secret",
    SCIM_URN_NAMESPACE="urn:ietf:dev",
)
class ScimOfferingUserOkTransitionTest(BaseScimTestCase):
    """Test trigger_scim_sync_on_offering_user_ok handler."""

    def setUp(self):
        self.user = structure_factories.UserFactory(username="user@example.org")
        self.offering = marketplace_factories.OfferingFactory()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_entitlements.delay")
    def test_triggers_sync_when_transitioning_to_ok_with_username(
        self, mock_sync_delay
    ):
        """SCIM sync is triggered when OfferingUser transitions to OK with username."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.user,
            offering=self.offering,
            username="",
            state=OfferingUserStates.CREATION_REQUESTED,
        )

        # Transition to OK with username
        offering_user.state = OfferingUserStates.OK
        offering_user.username = "posixuser"
        with self.captureOnCommitCallbacks(execute=True):
            offering_user.save()

        mock_sync_delay.assert_called_once_with(self.user.uuid.hex)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_entitlements.delay")
    def test_triggers_sync_when_transitioning_from_creating_to_ok(
        self, mock_sync_delay
    ):
        """SCIM sync is triggered when transitioning from CREATING to OK."""
        # Create without username to avoid auto-transition to OK
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.user,
            offering=self.offering,
            username="",
            state=OfferingUserStates.CREATING,
        )
        offering_user.refresh_from_db()

        offering_user.state = OfferingUserStates.OK
        offering_user.username = "posixuser"
        with self.captureOnCommitCallbacks(execute=True):
            offering_user.save()

        mock_sync_delay.assert_called_once_with(self.user.uuid.hex)

    @mock.patch("waldur_core.users.scim.tasks.sync_user_entitlements.delay")
    def test_no_sync_on_creation(self, mock_sync_delay):
        """No SCIM sync is triggered when OfferingUser is created."""
        marketplace_models.OfferingUser.objects.create(
            user=self.user,
            offering=self.offering,
            username="posixuser",
            state=OfferingUserStates.OK,
        )

        mock_sync_delay.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_entitlements.delay")
    def test_no_sync_when_state_unchanged(self, mock_sync_delay):
        """No SCIM sync when state doesn't change."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.user,
            offering=self.offering,
            username="posixuser",
            state=OfferingUserStates.OK,
        )

        offering_user.username = "newusername"
        with self.captureOnCommitCallbacks(execute=True):
            offering_user.save()

        mock_sync_delay.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_entitlements.delay")
    def test_no_sync_when_transitioning_away_from_ok(self, mock_sync_delay):
        """No SCIM sync when transitioning away from OK state."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.user,
            offering=self.offering,
            username="posixuser",
            state=OfferingUserStates.OK,
        )

        offering_user.state = OfferingUserStates.DELETION_REQUESTED
        with self.captureOnCommitCallbacks(execute=True):
            offering_user.save()

        mock_sync_delay.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_entitlements.delay")
    def test_no_sync_when_username_empty(self, mock_sync_delay):
        """No SCIM sync when transitioning to OK but username is empty."""
        offering_user = marketplace_models.OfferingUser.objects.create(
            user=self.user,
            offering=self.offering,
            username="",
            state=OfferingUserStates.CREATION_REQUESTED,
        )

        offering_user.state = OfferingUserStates.OK
        with self.captureOnCommitCallbacks(execute=True):
            offering_user.save()

        mock_sync_delay.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_entitlements.delay")
    def test_no_sync_when_scim_disabled(self, mock_sync_delay):
        """No SCIM sync when SCIM is disabled."""
        with override_config(SCIM_MEMBERSHIP_SYNC_ENABLED=False):
            offering_user = marketplace_models.OfferingUser.objects.create(
                user=self.user,
                offering=self.offering,
                username="",
                state=OfferingUserStates.CREATION_REQUESTED,
            )

            offering_user.state = OfferingUserStates.OK
            offering_user.username = "posixuser"
            offering_user.save()

        mock_sync_delay.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_user_entitlements.delay")
    def test_no_sync_when_scim_not_configured(self, mock_sync_delay):
        """No SCIM sync when SCIM is not configured."""
        with override_config(SCIM_API_URL=""):
            offering_user = marketplace_models.OfferingUser.objects.create(
                user=self.user,
                offering=self.offering,
                username="",
                state=OfferingUserStates.CREATION_REQUESTED,
            )

            offering_user.state = OfferingUserStates.OK
            offering_user.username = "posixuser"
            offering_user.save()

        mock_sync_delay.assert_not_called()


@override_config(
    SCIM_MEMBERSHIP_SYNC_ENABLED=True,
    SCIM_API_URL="https://scim.example.org",
    SCIM_API_KEY="secret",
    SCIM_URN_NAMESPACE="urn:ietf:dev",
)
class ScimResourceOkTransitionTest(BaseScimTestCase):
    """Test trigger_scim_sync_on_resource_ok handler."""

    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.offering = marketplace_factories.OfferingFactory()

    @mock.patch("waldur_core.users.scim.tasks.sync_users_for_offering_endpoint.delay")
    def test_triggers_sync_when_transitioning_to_ok(self, mock_sync_delay):
        resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            state=ResourceStates.CREATING,
        )

        resource.state = ResourceStates.OK
        with self.captureOnCommitCallbacks(execute=True):
            resource.save()

        mock_sync_delay.assert_called_once_with(self.offering.uuid.hex)

    @mock.patch("waldur_core.users.scim.tasks.sync_users_for_offering_endpoint.delay")
    def test_no_sync_when_state_unchanged(self, mock_sync_delay):
        marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            state=ResourceStates.OK,
        )

        mock_sync_delay.assert_not_called()

    @mock.patch("waldur_core.users.scim.tasks.sync_users_for_offering_endpoint.delay")
    def test_no_sync_when_scim_disabled(self, mock_sync_delay):
        resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.offering,
            state=ResourceStates.CREATING,
        )

        with override_config(SCIM_MEMBERSHIP_SYNC_ENABLED=False):
            resource.state = ResourceStates.OK
            with self.captureOnCommitCallbacks(execute=True):
                resource.save()

        mock_sync_delay.assert_not_called()
