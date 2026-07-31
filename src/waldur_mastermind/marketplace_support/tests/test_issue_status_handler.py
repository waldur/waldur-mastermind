from functools import cached_property
from unittest import mock

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.factories import ProjectFactory
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import handlers
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests.base import BaseTest


class SupportFixture:
    def __init__(self):
        self.success_issue_status
        self.fail_issue_status

    @cached_property
    def success_issue_status(self):
        return support_factories.IssueStatusFactory(
            name="Completed",
            type=support_models.IssueStatus.Types.RESOLVED,
        )

    @cached_property
    def second_success_issue_status(self):
        return support_factories.IssueStatusFactory(
            name="Done",
            type=support_models.IssueStatus.Types.RESOLVED,
        )

    @cached_property
    def fail_issue_status(self):
        return support_factories.IssueStatusFactory(
            name="Cancelled",
            type=support_models.IssueStatus.Types.CANCELED,
        )

    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING)

    @cached_property
    def project(self):
        return ProjectFactory()

    @cached_property
    def resource(self):
        return marketplace_factories.ResourceFactory(
            offering=self.offering, project=self.project
        )

    @cached_property
    def issue(self):
        return support_factories.IssueFactory(resource=self.order)

    @cached_property
    def order(self):
        return marketplace_factories.OrderFactory(
            project=self.project,
            state=OrderStates.EXECUTING,
            offering=self.offering,
            resource=self.resource,
        )


class IssueStatusHandlerTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = SupportFixture()

    def _make_restore_issue(self):
        """Build the state left behind by ResourceViewSet.restore.

        The resource has already been moved to CREATING and a RESTORE order is
        executing, with a restoration ticket attached to it.
        """
        resource = marketplace_factories.ResourceFactory(
            offering=self.fixture.offering,
            project=self.fixture.project,
            state=ResourceStates.CREATING,
        )
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=OrderStates.EXECUTING,
            offering=self.fixture.offering,
            resource=resource,
            type=OrderTypes.RESTORE,
        )
        return resource, order, support_factories.IssueFactory(resource=order)

    def test_order_is_done_when_restore_issue_is_resolved(self):
        """Regression: RESOURCE_CALLBACKS had no RESTORE entries, so resolving
        a restoration ticket raised KeyError: (4, True) out of post_save."""
        resource, order, issue = self._make_restore_issue()

        issue.status = self.fixture.success_issue_status.name
        issue.save()

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)

        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.OK)

    def test_resource_returns_to_terminated_when_restore_issue_is_canceled(self):
        resource, order, issue = self._make_restore_issue()

        issue.status = self.fixture.fail_issue_status.name
        issue.save()

        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.CANCELED)

        # Back to TERMINATED rather than ERRED, so the user can ask again.
        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.TERMINATED)

    def test_processing_log_records_restore_callback(self):
        _, _, issue = self._make_restore_issue()

        issue.status = self.fixture.success_issue_status.name
        issue.save()

        issue.refresh_from_db()
        callback_events = [
            e for e in issue.processing_log if e.get("event") == "callback_invoked"
        ]
        self.assertEqual(len(callback_events), 1)
        self.assertEqual(
            callback_events[0]["details"]["callback"], "resource_restore_succeeded"
        )

    def test_order_is_done_when_resource_creation_issue_is_resolved(self):
        self.fixture.issue.status = self.fixture.success_issue_status.name
        self.fixture.issue.save()

        self.fixture.order.refresh_from_db()
        self.assertEqual(self.fixture.order.state, OrderStates.DONE)

        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.fixture.resource.state, ResourceStates.OK)

    def test_order_is_terminated_when_resource_creation_issue_is_canceled(self):
        self.fixture.issue.status = self.fixture.fail_issue_status.name
        self.fixture.issue.save()

        self.fixture.order.refresh_from_db()
        self.assertEqual(
            self.fixture.order.state,
            OrderStates.CANCELED,
        )

        self.fixture.resource.refresh_from_db()
        self.assertEqual(self.fixture.resource.state, ResourceStates.TERMINATED)

    def test_use_second_resolve_state(self):
        self.fixture.issue.status = self.fixture.success_issue_status.name
        self.fixture.issue.save()

        self.fixture.order.refresh_from_db()
        self.assertEqual(self.fixture.order.state, OrderStates.DONE)

        self.fixture.issue.status = self.fixture.second_success_issue_status.name
        self.fixture.issue.save()

    def test_processing_log_is_populated_on_successful_resolution(self):
        """Test that processing_log captures status change and callback events."""
        self.fixture.issue.status = self.fixture.success_issue_status.name
        self.fixture.issue.save()

        self.fixture.issue.refresh_from_db()
        processing_log = self.fixture.issue.processing_log

        self.assertIsInstance(processing_log, list)
        self.assertGreaterEqual(len(processing_log), 2)

        # Check for status_changed event
        status_changed_events = [
            e for e in processing_log if e.get("event") == "status_changed"
        ]
        self.assertEqual(len(status_changed_events), 1)
        self.assertEqual(
            status_changed_events[0]["details"]["new_status"],
            self.fixture.success_issue_status.name,
        )
        self.assertTrue(status_changed_events[0]["details"]["resolved_value"])

        # Check for callback events
        callback_events = [
            e for e in processing_log if e.get("event") == "callback_invoked"
        ]
        self.assertEqual(len(callback_events), 1)
        self.assertEqual(
            callback_events[0]["details"]["callback"], "resource_creation_succeeded"
        )

    def test_processing_log_is_populated_on_cancellation(self):
        """Test that processing_log captures cancellation events."""
        self.fixture.issue.status = self.fixture.fail_issue_status.name
        self.fixture.issue.save()

        self.fixture.issue.refresh_from_db()
        processing_log = self.fixture.issue.processing_log

        self.assertIsInstance(processing_log, list)
        self.assertGreaterEqual(len(processing_log), 2)

        # Check for status_changed event with resolved=False
        status_changed_events = [
            e for e in processing_log if e.get("event") == "status_changed"
        ]
        self.assertEqual(len(status_changed_events), 1)
        self.assertFalse(status_changed_events[0]["details"]["resolved_value"])

        # Check for callback events
        callback_events = [
            e for e in processing_log if e.get("event") == "callback_invoked"
        ]
        self.assertEqual(len(callback_events), 1)
        self.assertEqual(
            callback_events[0]["details"]["callback"], "resource_creation_canceled"
        )

    def test_processing_log_captures_skipped_processing(self):
        """Test that processing_log captures when processing is skipped due to missing IssueStatus."""
        # Delete all IssueStatus to simulate misconfiguration
        support_models.IssueStatus.objects.all().delete()

        self.fixture.issue.status = "Unknown Status"
        self.fixture.issue.save()

        self.fixture.issue.refresh_from_db()
        processing_log = self.fixture.issue.processing_log

        self.assertIsInstance(processing_log, list)
        self.assertGreaterEqual(len(processing_log), 1)

        # Check for processing_skipped event
        skipped_events = [
            e for e in processing_log if e.get("event") == "processing_skipped"
        ]
        self.assertEqual(len(skipped_events), 1)
        self.assertIn("resolved_is_none", skipped_events[0]["details"]["reasons"])

    def test_missing_callback_is_logged_instead_of_raising(self):
        """An unmapped (order type, resolved) pair must not break the webhook.

        The handler runs inside issue.save(), which runs inside the helpdesk
        webhook request, so an exception here would return a 500 to the
        ticketing system and leave the order stuck.
        """
        with mock.patch.dict(handlers.RESOURCE_CALLBACKS, {}, clear=True):
            self.fixture.issue.status = self.fixture.success_issue_status.name
            self.fixture.issue.save()

        self.fixture.order.refresh_from_db()
        self.assertEqual(self.fixture.order.state, OrderStates.EXECUTING)

        self.fixture.issue.refresh_from_db()
        missing_events = [
            e
            for e in self.fixture.issue.processing_log
            if e.get("event") == "callback_missing"
        ]
        self.assertEqual(len(missing_events), 1)


class ProcessingLogVisibilityTest(test.APITestCase):
    """Test that processing_log is only visible to staff users."""

    def setUp(self):
        # Create IssueStatus entries
        support_models.IssueStatus.objects.get_or_create(
            name="done", defaults={"type": support_models.IssueStatus.Types.RESOLVED}
        )
        support_models.IssueStatus.objects.get_or_create(
            name="rejected",
            defaults={"type": support_models.IssueStatus.Types.CANCELED},
        )

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        self.regular_user = structure_factories.UserFactory()

        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)

        # Create issue with processing_log data
        self.issue = support_factories.IssueFactory(
            customer=self.customer,
            project=self.project,
            caller=self.regular_user,
            processing_log=[
                {
                    "timestamp": "2024-01-15T10:00:00Z",
                    "event": "status_changed",
                    "details": {"old_status": "Open", "new_status": "Done"},
                }
            ],
        )

    def _get_issue_url(self):
        return f"/api/support-issues/{self.issue.uuid}/"

    def test_staff_can_see_processing_log(self):
        """Staff users should see the processing_log field."""
        self.client.force_authenticate(self.staff)
        response = self.client.get(self._get_issue_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("processing_log", response.data)
        self.assertEqual(len(response.data["processing_log"]), 1)
        self.assertEqual(response.data["processing_log"][0]["event"], "status_changed")

    def test_support_can_see_processing_log(self):
        """Support users should see the processing_log field."""
        self.client.force_authenticate(self.support)
        response = self.client.get(self._get_issue_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("processing_log", response.data)
        self.assertEqual(len(response.data["processing_log"]), 1)

    def test_regular_user_cannot_see_processing_log(self):
        """Regular users should not see the processing_log field."""
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self._get_issue_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("processing_log", response.data)
