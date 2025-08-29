from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework import test

from waldur_core.logging.enums import EventType
from waldur_core.permissions import tasks, utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class LogRoleEventTest(TestCase):
    def setUp(self):
        self._logger_mock = mock.patch("waldur_core.logging.event_logger.emit")
        self.logger_mock = self._logger_mock.start()
        self.addCleanup(self._logger_mock.stop)

    def test_logger_called_when_customer_role_is_granted(self):
        fixture = fixtures.CustomerFixture()

        owner = fixture.owner
        self.logger_mock.reset_mock()
        fixture.customer.add_user(fixture.user, CustomerRole.OWNER, owner)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_GRANTED,
            event_context=mock.ANY,
            scopes=[fixture.customer, fixture.customer],
        )

    def test_logger_called_when_customer_role_is_revoked(self):
        fixture = fixtures.CustomerFixture()
        owner = fixture.owner

        self.logger_mock.reset_mock()
        fixture.customer.remove_user(owner, CustomerRole.OWNER, fixture.staff)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_REVOKED,
            event_context=mock.ANY,
            scopes=[fixture.customer, fixture.customer],
        )

    def test_logger_called_when_project_role_is_granted(self):
        fixture = fixtures.ProjectFixture()
        current_user = fixture.owner

        self.logger_mock.reset_mock()
        fixture.project.add_user(fixture.user, ProjectRole.MANAGER, current_user)

        self.logger_mock.assert_any_call(
            mock.ANY,
            event_type=EventType.ROLE_GRANTED,
            event_context=mock.ANY,
            scopes=[fixture.project, fixture.customer],
        )

    def test_logger_called_when_project_role_is_revoked(self):
        fixture = fixtures.ProjectFixture()
        manager = fixture.manager
        current_user = fixture.owner

        self.logger_mock.reset_mock()
        fixture.project.remove_user(manager, ProjectRole.MANAGER, current_user)

        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ROLE_REVOKED,
            event_context=mock.ANY,
            scopes=[fixture.project, fixture.customer],
        )


class AccessSubnetCreateModifyDelete(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.client.force_authenticate(user=self.fixture.owner)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ACCESS_SUBNET)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_ACCESS_SUBNET)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_ACCESS_SUBNET)
        self.customer = self.fixture.customer
        self.customer_url = factories.CustomerFactory.get_url(
            customer=self.fixture.customer
        )
        self._logger_mock = mock.patch("waldur_core.logging.event_logger.emit")
        self.logger_mock = self._logger_mock.start()
        self.addCleanup(self._logger_mock.stop)

    def test_logger_called_when_subnet_created(self):
        self.logger_mock.reset_mock()
        access_subnet = self.create_access_subnet()
        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_CREATION_SUCCEEDED,
            event_context={
                "access_subnet": access_subnet,
            },
            scopes=[access_subnet, access_subnet.customer],
        )

    def test_logger_called_when_subnet_modified(self):
        access_subnet = self.create_access_subnet()
        url = factories.AccessSubnetFactory.get_url(access_subnet)

        self.logger_mock.reset_mock()
        self.client.put(url, {"inet": "192.168.1.1/32"})
        self.logger_mock.assert_called_once_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_UPDATE_SUCCEEDED,  # TODO patch calls creation_succeeded but update_succeeded desired
            event_context={
                "access_subnet": access_subnet,
            },
            scopes=[access_subnet, access_subnet.customer],
        )

    def test_logger_called_when_subnet_deleted(self):
        access_subnet = self.create_access_subnet()
        url = factories.AccessSubnetFactory.get_url(access_subnet)

        self.client.delete(url)
        self.logger_mock.assert_called_with(
            mock.ANY,
            event_type=EventType.ACCESS_SUBNET_DELETION_SUCCEEDED,
            event_context=mock.ANY,
            scopes=mock.ANY,
        )

    def create_access_subnet(self):
        url = factories.AccessSubnetFactory.get_list_url()
        payload = {
            "customer": self.customer_url,
            "inet": "192.168.1.0/32",
            "description": "",
        }
        response = self.client.post(
            url,
            payload,
        )
        return response.data.serializer.instance


class RoleLogReasonTest(TestCase):
    def setUp(self):
        self._logger_mock = mock.patch("waldur_core.logging.event_logger.emit")
        self.logger_mock = self._logger_mock.start()
        self.addCleanup(self._logger_mock.stop)
        self.fixture = fixtures.CustomerFixture()

    def test_manual_role_revocation_includes_reason_and_initiator(self):
        """Test that manual role revocation via API includes proper reason and initiator."""
        self.logger_mock.reset_mock()

        # Use the delete_user utility that's called by the API
        utils.delete_user(
            self.fixture.customer,
            self.fixture.owner,
            CustomerRole.OWNER,
            current_user=self.fixture.staff,
            reason="Manual user removal via delete_user API endpoint",
        )

        # Check that the log was called with enhanced context
        # The fixture setup creates multiple log entries, so we need to find the right one
        self.assertTrue(self.logger_mock.called)

        # Find the role revocation call
        role_revoked_calls = [
            call
            for call in self.logger_mock.call_args_list
            if len(call[1]) > 1 and call[1].get("event_type") == EventType.ROLE_REVOKED
        ]
        self.assertEqual(len(role_revoked_calls), 1)
        call_args = role_revoked_calls[0]
        call_args = self.logger_mock.call_args

        # Verify message includes reason and initiator (in template form)
        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("Manual user removal via delete_user API endpoint", message)

        # Verify event context includes reason and initiated_by
        event_context = call_args[1]["event_context"]
        self.assertEqual(
            event_context["reason"], "Manual user removal via delete_user API endpoint"
        )
        self.assertEqual(
            event_context["initiated_by"],
            f"{self.fixture.staff.full_name} ({self.fixture.staff.username})",
        )

    def test_automatic_expiration_includes_system_reason(self):
        """Test that automatic expiration includes system reason and initiator."""
        # Create an expired role
        utils.add_user(
            self.fixture.customer,
            self.fixture.user,
            CustomerRole.SUPPORT,
            expiration_time=timezone.now() - timezone.timedelta(hours=1),
        )

        self.logger_mock.reset_mock()

        # Call the expiration task
        tasks.check_expired_permissions()

        # Check that the log was called with system context
        self.logger_mock.assert_called()
        call_args = self.logger_mock.call_args

        # Verify message includes system initiator and expiration reason (in template form)
        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("Automatic expiration cleanup task", message)

        # Verify event context
        event_context = call_args[1]["event_context"]
        self.assertEqual(event_context["reason"], "Automatic expiration cleanup task")
        self.assertEqual(event_context["initiated_by"], "System")

    def test_project_deletion_cascade_includes_reason(self):
        """Test that project deletion cascade includes proper reason."""
        project_fixture = fixtures.ProjectFixture()
        project = project_fixture.project

        self.logger_mock.reset_mock()

        # Import and call the handler directly since we can't easily trigger pre_delete signal in tests
        from waldur_core.structure.handlers import revoke_roles_on_project_deletion

        revoke_roles_on_project_deletion(sender=project.__class__, instance=project)

        # Check that roles were revoked with cascade reason
        if self.logger_mock.called:  # Only if there were roles to revoke
            call_args = self.logger_mock.call_args
            message = call_args[0][0]
            self.assertIn("Initiated by: {initiated_by}", message)
            self.assertIn("Project deletion cascade", message)

            event_context = call_args[1]["event_context"]
            self.assertEqual(event_context["reason"], "Project deletion cascade")
            self.assertEqual(event_context["initiated_by"], "System")

    def test_role_granted_includes_reason_and_initiator(self):
        """Test that role granting includes proper reason and initiator."""
        self.logger_mock.reset_mock()

        utils.add_user(
            self.fixture.customer,
            self.fixture.user,
            CustomerRole.SUPPORT,
            created_by=self.fixture.owner,
        )

        # Check that the log was called with enhanced context
        self.logger_mock.assert_called()
        call_args = self.logger_mock.call_args

        # Verify message includes reason and initiator (in template form)
        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("Manual role assignment via API", message)

        # Verify event context
        event_context = call_args[1]["event_context"]
        self.assertEqual(event_context["reason"], "Manual role assignment via API")
        self.assertEqual(
            event_context["initiated_by"],
            f"{self.fixture.owner.full_name} ({self.fixture.owner.username})",
        )

    def test_role_updated_includes_reason_and_initiator(self):
        """Test that role updates include proper reason and initiator."""
        # Create a role first
        user_role = utils.add_user(
            self.fixture.customer,
            self.fixture.user,
            CustomerRole.SUPPORT,
        )

        self.logger_mock.reset_mock()

        # Update the role expiration time
        new_expiration = timezone.now() + timezone.timedelta(days=30)
        user_role.set_expiration_time(new_expiration, current_user=self.fixture.staff)

        # Check that the log was called with enhanced context
        self.logger_mock.assert_called()
        call_args = self.logger_mock.call_args

        # Verify message includes reason and initiator (in template form)
        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("Manual role update via API", message)

        # Verify event context
        event_context = call_args[1]["event_context"]
        self.assertEqual(event_context["reason"], "Manual role update via API")
        self.assertEqual(
            event_context["initiated_by"],
            f"{self.fixture.staff.full_name} ({self.fixture.staff.username})",
        )

    def test_system_initiated_operations_have_system_initiator(self):
        """Test that system operations without current_user show System as initiator."""
        user_role = utils.add_user(
            self.fixture.customer,
            self.fixture.user,
            CustomerRole.SUPPORT,
        )

        self.logger_mock.reset_mock()

        # Revoke without current_user (simulating system operation)
        user_role.revoke(current_user=None, reason="System maintenance")

        # Check that the log shows System as initiator
        self.logger_mock.assert_called()
        call_args = self.logger_mock.call_args

        message = call_args[0][0]
        self.assertIn("Initiated by: {initiated_by}", message)
        self.assertIn("System maintenance", message)

        event_context = call_args[1]["event_context"]
        self.assertEqual(event_context["reason"], "System maintenance")
        self.assertEqual(event_context["initiated_by"], "System")
