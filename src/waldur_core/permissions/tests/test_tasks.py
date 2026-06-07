from constance.test.unittest import override_config
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from waldur_core.logging import models as logging_models
from waldur_core.permissions import tasks
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.handlers import (
    deactivate_user_with_logging,
    reactivate_user_with_logging,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import CourseAccountState
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


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

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_does_not_deactivate_users_with_active_course_accounts(self):
        """Test that users with active course accounts in non-removed projects are not deactivated."""
        user_with_course = structure_factories.UserFactory(is_active=True)
        self.assertFalse(user_with_course.userrole_set.filter(is_active=True).exists())

        marketplace_factories.CourseAccountFactory(
            user=user_with_course,
            state=CourseAccountState.OK,
        )

        tasks.sync_user_deactivation_status()

        user_with_course.refresh_from_db()
        self.assertTrue(user_with_course.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_reactivates_inactive_users_with_ok_course_accounts(self):
        """Test that inactive users with OK course accounts in active projects are reactivated."""
        user_with_course = structure_factories.UserFactory(is_active=False)
        self.assertFalse(user_with_course.userrole_set.filter(is_active=True).exists())

        marketplace_factories.CourseAccountFactory(
            user=user_with_course,
            state=CourseAccountState.OK,
        )

        tasks.sync_user_deactivation_status()

        user_with_course.refresh_from_db()
        self.assertTrue(user_with_course.is_active)
        self.assertEqual(user_with_course.deactivation_reason, "")

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_does_not_reactivate_users_with_only_closed_course_accounts(self):
        """Test that inactive users with only closed course accounts stay inactive."""
        user_with_closed_course = structure_factories.UserFactory(is_active=False)
        self.assertFalse(
            user_with_closed_course.userrole_set.filter(is_active=True).exists()
        )

        marketplace_factories.CourseAccountFactory(
            user=user_with_closed_course,
            state=CourseAccountState.CLOSED,
        )

        tasks.sync_user_deactivation_status()

        user_with_closed_course.refresh_from_db()
        self.assertFalse(user_with_closed_course.is_active)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_deactivates_users_with_closed_course_accounts(self):
        """Test that users with only closed course accounts are still deactivated."""
        user_with_closed_course = structure_factories.UserFactory(is_active=True)
        self.assertFalse(
            user_with_closed_course.userrole_set.filter(is_active=True).exists()
        )

        marketplace_factories.CourseAccountFactory(
            user=user_with_closed_course,
            state=CourseAccountState.CLOSED,
        )

        tasks.sync_user_deactivation_status()

        user_with_closed_course.refresh_from_db()
        self.assertFalse(user_with_closed_course.is_active)


class DeactivationReasonTest(TestCase):
    """Tests that deactivation_reason is set and cleared across all code paths."""

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_deactivate_user_with_logging_sets_reason(self):
        user = structure_factories.UserFactory(is_active=True)
        deactivate_user_with_logging(user, "All roles were revoked")
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(user.deactivation_reason, "All roles were revoked")

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_reactivate_user_with_logging_clears_reason(self):
        user = structure_factories.UserFactory(
            is_active=False, deactivation_reason="All roles were revoked"
        )
        reactivate_user_with_logging(user, "Gained a new role")
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user.deactivation_reason, "")

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_periodic_sync_sets_descriptive_reason_no_course_accounts(self):
        user = structure_factories.UserFactory(is_active=True)
        tasks.sync_user_deactivation_status()
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(
            user.deactivation_reason, "No active roles and no course accounts"
        )

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_periodic_sync_sets_descriptive_reason_with_closed_course_accounts(self):
        user = structure_factories.UserFactory(is_active=True)
        marketplace_factories.CourseAccountFactory(
            user=user, state=CourseAccountState.CLOSED
        )
        tasks.sync_user_deactivation_status()
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertIn(
            "1 course account(s) but none in OK state", user.deactivation_reason
        )

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_periodic_sync_clears_reason_on_reactivated_users(self):
        user = structure_factories.UserFactory(
            is_active=False,
            deactivation_reason="No active roles and no course accounts",
        )
        customer = structure_factories.CustomerFactory()
        customer.add_user(user, CustomerRole.OWNER)
        # Manually set inactive after role assignment to avoid handler interference
        user.is_active = False
        user.deactivation_reason = "No active roles and no course accounts"
        user.save()

        tasks.sync_user_deactivation_status()

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user.deactivation_reason, "")

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_signal_handler_sets_reason_on_last_role_revoked(self):
        user = structure_factories.UserFactory(is_active=True)
        customer = structure_factories.CustomerFactory()
        customer.add_user(user, CustomerRole.OWNER)

        # Revoke the role — triggers deactivate_user_if_no_roles signal handler
        role = user.userrole_set.get(is_active=True)
        role.revoke()

        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(
            user.deactivation_reason, "No active roles and no course accounts"
        )

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_signal_handler_clears_reason_on_role_granted(self):
        user = structure_factories.UserFactory(
            is_active=False, deactivation_reason="All roles were revoked"
        )
        customer = structure_factories.CustomerFactory()

        # Granting a role triggers reactivate_user_if_gaining_roles signal handler
        customer.add_user(user, CustomerRole.OWNER)

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user.deactivation_reason, "")


class SyncUserDeactivationQueryCountTest(TestCase):
    """Regression test: the periodic task must not scale linearly with the
    number of users. Previously it issued one ``UserRole.exists()`` and one
    ``CourseAccount.exists()`` per user, which dominated runtime on
    production-sized tables (see Sentry transaction
    ``waldur_core.permissions.sync_user_deactivation_status``)."""

    def _make_noop_users(self, n: int) -> None:
        """Create N users that already match the desired state, so the task
        has no work to do regardless of the user count."""
        customer = structure_factories.CustomerFactory()
        for _ in range(n):
            # Active user with an active role — neither deactivation nor
            # reactivation should fire for this user.
            user = structure_factories.UserFactory(is_active=True)
            customer.add_user(user, CustomerRole.OWNER)

    def _count_queries(self) -> int:
        with CaptureQueriesContext(connection) as ctx:
            tasks.sync_user_deactivation_status()
        return len(ctx.captured_queries)

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_query_count_is_bounded_for_noop_users(self):
        self._make_noop_users(5)
        small_count = self._count_queries()

        self._make_noop_users(45)  # 50 total
        large_count = self._count_queries()

        # Bulk Exists() filtering pushes role/course-account checks into
        # SQL, so query count must stay flat as the user table grows.
        # Allow a small slack for chunk pagination and Constance lookups,
        # but reject anything that scales with N.
        self.assertLess(
            large_count,
            small_count + 10,
            f"Query count grew from {small_count} to {large_count} when user "
            f"count went from 5 to 50 — likely a per-user N+1 regression.",
        )
