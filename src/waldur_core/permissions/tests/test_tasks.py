from constance.test.unittest import override_config
from django.test import TestCase

from waldur_core.logging import models as logging_models
from waldur_core.permissions import tasks
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories


class SyncUserDeactivationStatusTest(TestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(is_active=True)
        self.user_with_role = structure_factories.UserFactory(is_active=True)
        self.staff_user = structure_factories.UserFactory(is_staff=True, is_active=True)
        self.support_user = structure_factories.UserFactory(
            is_support=True, is_active=True
        )

        # Create customer and assign role to user_with_role
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.user_with_role, CustomerRole.OWNER)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=False)
    def test_task_skips_when_setting_disabled(self):
        """Test that task does nothing when DEACTIVATE_USER_IF_NO_ROLES is disabled."""
        initial_active_status = self.user.is_active

        tasks.sync_user_deactivation_status()

        self.user.refresh_from_db()
        self.assertEqual(self.user.is_active, initial_active_status)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_deactivates_users_with_no_roles(self):
        """Test that users with no active roles are deactivated."""
        # Ensure user has no roles
        self.assertFalse(self.user.userrole_set.filter(is_active=True).exists())
        self.assertTrue(self.user.is_active)

        tasks.sync_user_deactivation_status()

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

        # Check that deactivation event was logged
        self.assertTrue(
            logging_models.Event.objects.filter(
                event_type="user_deactivated_no_roles"
            ).exists()
        )

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_does_not_deactivate_users_with_active_roles(self):
        """Test that users with active roles remain active."""
        # user_with_role has a role assigned in setUp
        self.assertTrue(
            self.user_with_role.userrole_set.filter(is_active=True).exists()
        )
        self.assertTrue(self.user_with_role.is_active)

        tasks.sync_user_deactivation_status()

        self.user_with_role.refresh_from_db()
        self.assertTrue(self.user_with_role.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_does_not_deactivate_staff_users(self):
        """Test that staff users are never deactivated, even without roles."""
        self.assertFalse(self.staff_user.userrole_set.filter(is_active=True).exists())
        self.assertTrue(self.staff_user.is_active)
        self.assertTrue(self.staff_user.is_staff)

        tasks.sync_user_deactivation_status()

        self.staff_user.refresh_from_db()
        self.assertTrue(self.staff_user.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_does_not_deactivate_support_users(self):
        """Test that support users are never deactivated, even without roles."""
        self.assertFalse(self.support_user.userrole_set.filter(is_active=True).exists())
        self.assertTrue(self.support_user.is_active)
        self.assertTrue(self.support_user.is_support)

        tasks.sync_user_deactivation_status()

        self.support_user.refresh_from_db()
        self.assertTrue(self.support_user.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_reactivates_users_with_roles_who_are_inactive(self):
        """Test that inactive users with active roles are reactivated."""
        # Use a simplified approach: directly test the handler functions
        from waldur_core.permissions.handlers import (
            reactivate_user_with_logging,
            should_reactivate_user,
        )

        # Set up user with role but inactive
        test_user = structure_factories.UserFactory(is_active=False)
        test_customer = structure_factories.CustomerFactory()
        test_customer.add_user(test_user, CustomerRole.OWNER)

        # Manually set inactive after role assignment to avoid handler interference
        test_user.is_active = False
        test_user.save()

        # Verify the conditions are correct
        self.assertTrue(should_reactivate_user(test_user))
        self.assertFalse(test_user.is_active)

        # Directly test the reactivation function
        reactivate_user_with_logging(test_user, "Test reactivation")

        # Verify reactivation worked
        test_user.refresh_from_db()
        self.assertTrue(test_user.is_active)

        # Check that activation event was logged
        self.assertTrue(
            logging_models.Event.objects.filter(event_type="user_activated").exists()
        )

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_does_not_reactivate_inactive_staff_users(self):
        """Test that inactive staff users are not reactivated even if they have roles."""
        # Make staff user inactive and give them a role
        self.staff_user.is_active = False
        self.staff_user.save()
        self.customer.add_user(self.staff_user, CustomerRole.OWNER)

        tasks.sync_user_deactivation_status()

        self.staff_user.refresh_from_db()
        # Should remain inactive because staff users are excluded from auto-reactivation
        self.assertFalse(self.staff_user.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_does_not_reactivate_inactive_support_users(self):
        """Test that inactive support users are not reactivated even if they have roles."""
        # Make support user inactive and give them a role
        self.support_user.is_active = False
        self.support_user.save()
        self.customer.add_user(self.support_user, CustomerRole.OWNER)

        tasks.sync_user_deactivation_status()

        self.support_user.refresh_from_db()
        # Should remain inactive because support users are excluded from auto-reactivation
        self.assertFalse(self.support_user.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_handles_mixed_scenarios_correctly(self):
        """Test that the task correctly handles multiple users with different scenarios."""
        # Create additional test users
        inactive_user_with_role = structure_factories.UserFactory(is_active=False)
        active_user_without_role = structure_factories.UserFactory(is_active=True)

        # Give role to inactive user
        self.customer.add_user(inactive_user_with_role, CustomerRole.OWNER)

        # Ensure active_user_without_role has no roles
        self.assertFalse(
            active_user_without_role.userrole_set.filter(is_active=True).exists()
        )

        tasks.sync_user_deactivation_status()

        # Refresh all users
        inactive_user_with_role.refresh_from_db()
        active_user_without_role.refresh_from_db()
        self.user_with_role.refresh_from_db()

        # Check results
        self.assertTrue(inactive_user_with_role.is_active)  # Should be reactivated
        self.assertFalse(active_user_without_role.is_active)  # Should be deactivated
        self.assertTrue(self.user_with_role.is_active)  # Should remain active

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_task_logs_summary_information(self):
        """Test that the task logs summary of actions taken."""
        # Create users that will be affected
        structure_factories.UserFactory(is_active=True)
        user_to_reactivate = structure_factories.UserFactory(is_active=False)
        customer = structure_factories.CustomerFactory()

        # Give role to user_to_reactivate
        customer.add_user(user_to_reactivate, CustomerRole.OWNER)
        # Manually deactivate again to ensure the test setup is correct
        user_to_reactivate.is_active = False
        user_to_reactivate.save()

        with self.assertLogs("waldur_core.permissions.tasks", level="INFO") as log:
            tasks.sync_user_deactivation_status()

        # Check that summary log message is present
        summary_logs = [log for log in log.output if "sync completed" in log]
        self.assertTrue(len(summary_logs) > 0)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_task_skips_during_import_operations(self):
        """Test that task skips execution during import operations."""
        from waldur_core.core.middleware import skip_side_effects

        # Create a user that would normally be deactivated
        user_without_roles = structure_factories.UserFactory(is_active=True)

        # Run the task within skip_rabbitmq_messages context (simulating import)
        with skip_side_effects():
            tasks.sync_user_deactivation_status()

        # User should remain active because task was skipped
        user_without_roles.refresh_from_db()
        self.assertTrue(user_without_roles.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_handlers_skip_during_import_operations(self):
        """Test that handlers skip execution during import operations."""
        from waldur_core.core.middleware import skip_side_effects

        # Create a user and customer for role assignment
        test_user = structure_factories.UserFactory(is_active=True)
        test_customer = structure_factories.CustomerFactory()

        # Simulate import context and add user role (this would normally trigger reactivation handler)
        with skip_side_effects():
            # This simulates what import_structure does - direct role creation
            test_customer.add_user(test_user, CustomerRole.OWNER)

        # Verify the role was created but handlers didn't interfere
        self.assertTrue(test_user.userrole_set.filter(is_active=True).exists())

        # Now test that handlers work normally outside import context
        test_user.is_active = False
        test_user.save()

        # Add another role - this should trigger reactivation handler
        # Use a different customer to create a separate role assignment
        test_customer2 = structure_factories.CustomerFactory()
        test_customer2.add_user(test_user, CustomerRole.OWNER)

        # User should be reactivated by handler
        test_user.refresh_from_db()
        self.assertTrue(test_user.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_no_action_when_all_users_have_correct_status(self):
        """Test that no action is taken when all users already have correct status."""
        # Ensure all regular users have roles and are active
        # (user_with_role already has this setup)

        # Manually deactivate users without roles
        self.user.is_active = False
        self.user.save()

        with self.assertLogs("waldur_core.permissions.tasks", level="INFO") as log:
            tasks.sync_user_deactivation_status()

        # Check that summary shows 0 actions
        summary_logs = [
            log
            for log in log.output
            if "sync completed" in log
            and "Deactivated: 0" in log
            and "Reactivated: 0" in log
        ]
        self.assertTrue(len(summary_logs) > 0)
