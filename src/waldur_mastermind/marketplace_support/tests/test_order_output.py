"""Tests for order output field population in ticket-based offerings."""

from unittest import mock

from django.test import TestCase

from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import handlers
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests import fixtures as support_fixtures


class OrderOutputTest(TestCase):
    def setUp(self):
        self.fixture = support_fixtures.SupportFixture()
        self.offering = marketplace_factories.OfferingFactory(
            type="Marketplace.Support",
            customer=self.fixture.customer,
        )
        self.order = marketplace_factories.OrderFactory(
            offering=self.offering,
            type=OrderTypes.TERMINATE,
            state=OrderStates.EXECUTING,
        )
        self.issue = support_factories.IssueFactory(
            resource=self.order,
            backend_name="JIRA",
            key="TEST-123",
            status="In Progress",
        )

        # Create issue statuses
        support_models.IssueStatus.objects.create(
            name="Done", type=support_models.IssueStatus.Types.RESOLVED
        )
        support_models.IssueStatus.objects.create(
            name="Canceled", type=support_models.IssueStatus.Types.CANCELED
        )

    def test_order_output_is_populated_when_issue_is_resolved(self):
        """Test that order.output is populated when issue status changes to resolved."""
        # Ensure the issue has a tracker that detects the change
        old_status = self.issue.status
        self.issue.status = "Done"
        # Manually set the tracker to detect the change
        self.issue.tracker.set_saved_fields({"status": old_status})
        self.issue.save()

        # Directly call the output update function to test it
        handlers._update_order_output_safely(self.order, self.issue)

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.output)
        self.assertIsNotNone(self.order.output_updated_at)

        # Verify the plain text output
        output_text = self.order.output
        self.assertIn("Issue: TEST-123 (JIRA)", output_text)
        self.assertIn("Status: Done", output_text)
        self.assertIn("Resolution: Success", output_text)
        self.assertIn("Updated:", output_text)

    def test_order_output_is_populated_when_issue_is_canceled(self):
        """Test that order.output is populated when issue status changes to canceled."""
        self.issue.status = "Canceled"
        self.issue.save()

        # Directly call the output update function
        handlers._update_order_output_safely(self.order, self.issue)

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.output)
        self.assertIsNotNone(self.order.output_updated_at)

        # Verify the plain text output
        output_text = self.order.output
        self.assertIn("Issue: TEST-123 (JIRA)", output_text)
        self.assertIn("Status: Canceled", output_text)
        self.assertIn("Resolution: Failed/Canceled", output_text)

    def test_public_comments_are_included_in_output(self):
        """Test that only public comments are included in the output."""
        # Create public and private comments
        support_factories.CommentFactory(
            issue=self.issue,
            description="Public update",
            is_public=True,
        )
        support_factories.CommentFactory(
            issue=self.issue,
            description="Private internal note",
            is_public=False,
        )

        self.issue.status = "Done"
        self.issue.save()

        # Directly call the output update function
        handlers._update_order_output_safely(self.order, self.issue)

        self.order.refresh_from_db()
        output_text = self.order.output

        # Check that public comment is included
        self.assertIn("Recent Updates:", output_text)
        self.assertIn("Public update", output_text)

        # Ensure private comment is not included
        self.assertNotIn("Private internal note", output_text)

    def test_no_personal_information_in_output(self):
        """Test that no personal information is included in the output."""
        # Add assignee and comment author
        support_user = support_factories.SupportUserFactory(
            name="John Doe",
            backend_name="JIRA",
        )
        self.issue.assignee = support_user
        self.issue.save()

        support_factories.CommentFactory(
            issue=self.issue,
            description="Update from support",
            is_public=True,
            author=support_user,
        )

        self.issue.status = "Done"
        self.issue.save()

        # Directly call the output update function
        handlers._update_order_output_safely(self.order, self.issue)

        self.order.refresh_from_db()
        output_str = self.order.output

        # Check that personal names are not in the output
        self.assertNotIn("John Doe", output_str)
        self.assertNotIn("author", output_str)

        # Check that we only have a generic assignment indicator
        self.assertIn("Assigned: Yes", output_str)

    def test_output_generation_failure_does_not_break_processing(self):
        """Test that failures in output generation don't break the main processing."""
        # Mock order.save to raise an exception
        with mock.patch.object(
            self.order, "save", side_effect=Exception("Save failed")
        ):
            with mock.patch(
                "waldur_mastermind.marketplace_support.handlers.logger"
            ) as mock_logger:
                self.issue.status = "Done"
                self.issue.save()

                # This should not raise an exception
                handlers._update_order_output_safely(self.order, self.issue)

                # Check that error was logged
                mock_logger.error.assert_called()
                error_message = str(mock_logger.error.call_args[0][0])
                self.assertIn("Failed to update output", error_message)

    def test_invalid_existing_output_is_handled_gracefully(self):
        """Test that output generation works regardless of existing content."""
        # Set some invalid content in output field
        self.order.output = "Invalid existing content"
        self.order.save()

        self.issue.status = "Done"
        self.issue.save()

        # This should not raise an exception
        handlers._update_order_output_safely(self.order, self.issue)

        self.order.refresh_from_db()
        # Should have valid text now
        output_text = self.order.output
        self.assertIn("Issue: TEST-123 (JIRA)", output_text)

    def test_webhook_updates_create_plain_text_output(self):
        """Test that webhook updates create plain text output."""
        from waldur_mastermind.support.views import SmaxWebHookReceiverView

        # Simulate webhook update
        view = SmaxWebHookReceiverView()
        view._update_order_output_from_webhook(
            self.order, self.issue, "SMAX", {"id": "12345"}
        )

        self.order.refresh_from_db()
        output_text = self.order.output

        # Check that webhook output is in plain text format
        self.assertIn("Issue: TEST-123 (SMAX)", output_text)
        self.assertIn("Webhook Events: 1", output_text)
