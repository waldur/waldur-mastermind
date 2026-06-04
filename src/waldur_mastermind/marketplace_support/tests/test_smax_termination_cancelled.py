"""Test SMAX termination order cancellation flow with output tracking."""

from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.test import TestCase
from rest_framework.test import APIClient

from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.backend.smax import SmaxServiceBackend
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests import fixtures as support_fixtures

_SMAX_WEBHOOK_SECRET = "smax-test-secret"  # noqa: S105


@override_constance_config(SMAX_WEBHOOK_SHARED_SECRET=_SMAX_WEBHOOK_SECRET)
class SmaxTerminationCancellationTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.fixture = support_fixtures.SupportFixture()

        # Create SMAX-backed offering
        self.offering = marketplace_factories.OfferingFactory(
            type="Marketplace.Support",
            customer=self.fixture.customer,
        )

        # Create termination order
        self.order = marketplace_factories.OrderFactory(
            offering=self.offering,
            type=OrderTypes.TERMINATE,
            state=OrderStates.EXECUTING,
        )

        # Create linked SMAX issue
        self.issue = support_factories.IssueFactory(
            resource=self.order,
            backend_name=SmaxServiceBackend.backend_name,
            backend_id="REQ0000123",
            key="REQ0000123",
            status="Work In Progress",
        )

        # Create issue statuses that SMAX might use
        support_models.IssueStatus.objects.create(
            name="Complete", type=support_models.IssueStatus.Types.RESOLVED
        )
        support_models.IssueStatus.objects.create(
            name="Cancelled", type=support_models.IssueStatus.Types.CANCELED
        )
        support_models.IssueStatus.objects.create(
            name="Rejected", type=support_models.IssueStatus.Types.CANCELED
        )

    @mock.patch(
        "waldur_mastermind.support.backend.smax.SmaxServiceBackend.update_waldur_issue_from_smax"
    )
    def test_termination_cancelled_in_smax_populates_order_output(self, mock_update):
        """Test that when SMAX cancels a termination request, order.output contains relevant info."""

        def mock_smax_update(issue):
            # Simulate SMAX updating the issue to "Cancelled" status
            issue.status = "Cancelled"
            issue.save()

        mock_update.side_effect = mock_smax_update

        # Simulate SMAX webhook payload for cancelled termination
        webhook_data = {
            "id": "REQ0000123",
            "Status": "Cancelled",
            "RequestedFor": "user@example.com",
            "Description": "Termination request has been cancelled due to business requirements",
        }

        # Call the webhook endpoint with the shared secret header (SEC-C7).
        response = self.client.post(
            "/api/support-smax-webhook/",
            data=webhook_data,
            format="json",
            HTTP_X_WEBHOOK_SECRET=_SMAX_WEBHOOK_SECRET,
        )

        self.assertEqual(response.status_code, 200)

        # Refresh from DB and check order output
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.output)

        # Check the plain text output
        output_text = self.order.output

        # Should show cancellation info from SMAX
        self.assertIn("Issue: REQ0000123 (SMAX)", output_text)
        self.assertIn(
            "Status: Cancelled", output_text
        )  # The SMAX status that caused cancellation
        self.assertIn("Webhook Events: 1", output_text)

        # Verify the issue.resolved property works correctly
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.status, "Cancelled")
        self.assertFalse(
            self.issue.resolved
        )  # Should be False for CANCELED type status

    def test_multiple_status_changes_tracked_in_output(self):
        """Test that multiple SMAX status changes are tracked in the order output."""
        from waldur_mastermind.support.views import SmaxWebHookReceiverView

        view = SmaxWebHookReceiverView()

        # First update: In Progress
        self.issue.status = "Work In Progress"
        view._update_order_output_from_webhook(
            self.order,
            self.issue,
            "SMAX",
            {"id": "REQ0000123", "Status": "Work In Progress"},
        )

        # Second update: Pending Approval
        self.issue.status = "Pending Approval"
        view._update_order_output_from_webhook(
            self.order,
            self.issue,
            "SMAX",
            {"id": "REQ0000123", "Status": "Pending Approval"},
        )

        # Final update: Cancelled
        self.issue.status = "Cancelled"
        view._update_order_output_from_webhook(
            self.order, self.issue, "SMAX", {"id": "REQ0000123", "Status": "Cancelled"}
        )

        # Check final output
        self.order.refresh_from_db()
        output_text = self.order.output

        # Should show final cancelled status
        self.assertIn("Status: Cancelled", output_text)
        self.assertIn("Issue: REQ0000123 (SMAX)", output_text)

        # Should have 3 webhook events tracked
        self.assertIn("Webhook Events: 3", output_text)

    def test_status_that_determines_cancellation_is_captured(self):
        """Test that the specific SMAX status that determines cancellation is captured."""
        from waldur_mastermind.marketplace_support import handlers

        # Test different SMAX statuses that should trigger cancellation
        cancellation_statuses = ["Cancelled", "Rejected", "Closed Unsuccessful"]

        for smax_status in cancellation_statuses:
            with self.subTest(status=smax_status):
                # Reset order output
                self.order.output = ""
                self.order.save()

                # Create corresponding IssueStatus if it doesn't exist
                support_models.IssueStatus.objects.get_or_create(
                    name=smax_status,
                    defaults={"type": support_models.IssueStatus.Types.CANCELED},
                )

                # Update issue to the cancellation status
                self.issue.status = smax_status
                self.issue.save()

                # Call our output update function
                handlers._update_order_output_safely(self.order, self.issue)

                # Check that the specific status is captured
                self.order.refresh_from_db()
                output_text = self.order.output

                # The exact SMAX status should be visible in output
                self.assertIn(f"Status: {smax_status}", output_text)
                self.assertIn("Resolution: Failed/Canceled", output_text)

                # This status should resolve to False (cancelled)
                self.assertFalse(self.issue.resolved)
