"""Test to verify that duplicate notifications are not sent for proposals already in review state."""

import unittest.mock as mock
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import tasks, utils
from waldur_mastermind.proposal.enums import CallStates, ProposalStates
from waldur_mastermind.proposal.models import Round
from waldur_mastermind.proposal.tests import factories as proposal_factories
from waldur_mastermind.proposal.tests import fixtures


class PreventDuplicateNotificationTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal

    @mock.patch(
        "waldur_mastermind.proposal.utils.tasks.notify_user_about_proposal_state_update"
    )
    @mock.patch("waldur_mastermind.proposal.utils.get_available_reviewer")
    def test_notification_not_sent_when_already_in_review(
        self, mock_get_reviewer, mock_notify
    ):
        """Test that notification is not sent when proposal is already IN_REVIEW."""
        # Set proposal to IN_REVIEW state
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()

        # Mock get_available_reviewer to return empty list (no new reviewers to assign)
        mock_get_reviewer.return_value = []

        # Call the function
        utils.process_proposals_pending_reviewers(self.proposal)

        # Verify that notification was NOT called
        mock_notify.delay.assert_not_called()

    @mock.patch(
        "waldur_mastermind.proposal.utils.tasks.notify_user_about_proposal_state_update"
    )
    @mock.patch("waldur_mastermind.proposal.utils.get_available_reviewer")
    def test_notification_sent_when_state_changes(self, mock_get_reviewer, mock_notify):
        """Test that notification is sent when proposal state changes from SUBMITTED to IN_REVIEW."""
        # Set proposal to SUBMITTED state
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()

        # Mock get_available_reviewer to return empty list
        mock_get_reviewer.return_value = []

        # Call the function
        utils.process_proposals_pending_reviewers(self.proposal)

        # Verify that notification WAS called with correct parameters
        mock_notify.delay.assert_called_once_with(
            self.proposal.uuid, ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW
        )

        # Verify proposal state was updated
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

    @mock.patch(
        "waldur_mastermind.proposal.utils.tasks.notify_user_about_proposal_state_update"
    )
    @mock.patch("waldur_mastermind.proposal.utils.get_available_reviewer")
    def test_no_duplicate_notifications_on_multiple_calls(
        self, mock_get_reviewer, mock_notify
    ):
        """Test that calling process_proposals_pending_reviewers multiple times doesn't send duplicate notifications."""
        # Set proposal to SUBMITTED state
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()

        # Mock get_available_reviewer to return empty list
        mock_get_reviewer.return_value = []

        # First call - should send notification
        utils.process_proposals_pending_reviewers(self.proposal)

        # Refresh proposal to get updated state
        self.proposal.refresh_from_db()

        # Second call - should NOT send notification
        utils.process_proposals_pending_reviewers(self.proposal)

        # Third call - should NOT send notification
        utils.process_proposals_pending_reviewers(self.proposal)

        # Verify that notification was called only once
        self.assertEqual(mock_notify.delay.call_count, 1)
        mock_notify.delay.assert_called_once_with(
            self.proposal.uuid, ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW
        )


class PeriodicTaskDuplicateNotificationTest(TestCase):
    """Test that periodic tasks don't send duplicate notifications."""

    def setUp(self):
        # Create fixtures manually to avoid auto-created proposals
        self.customer = structure_factories.CustomerFactory()
        self.call_manager = proposal_factories.CallManagingOrganisationFactory(
            customer=self.customer
        )
        self.call = proposal_factories.CallFactory(
            manager=self.call_manager, state=CallStates.DRAFT
        )
        self.round = proposal_factories.RoundFactory(call=self.call)
        self.proposal = proposal_factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT
        )

    @mock.patch(
        "waldur_mastermind.proposal.utils.tasks.notify_user_about_proposal_state_update"
    )
    @mock.patch("waldur_mastermind.proposal.utils.get_available_reviewer")
    def test_after_proposal_task_no_duplicate_notifications(
        self, mock_get_reviewer, mock_notify
    ):
        """Test that create_reviews_if_strategy_is_after_proposal doesn't send duplicate notifications."""
        # Setup: Set round to use AFTER_PROPOSAL strategy
        self.round.review_strategy = Round.ReviewStrategies.AFTER_PROPOSAL
        self.round.save()

        # Set call to ACTIVE state
        self.call.state = CallStates.ACTIVE
        self.call.save()

        # Set proposal to SUBMITTED state initially
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()

        # Mock get_available_reviewer to return empty list
        mock_get_reviewer.return_value = []

        # First run of the periodic task - should send notification
        tasks.create_reviews_if_strategy_is_after_proposal()

        # Verify notification was sent once
        self.assertEqual(mock_notify.delay.call_count, 1)
        mock_notify.delay.assert_called_with(
            self.proposal.uuid, ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW
        )

        # Refresh proposal to get updated state
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

        # Reset mock to track new calls
        mock_notify.delay.reset_mock()

        # Second run of the periodic task - should NOT send notification
        tasks.create_reviews_if_strategy_is_after_proposal()

        # Verify no new notification was sent
        mock_notify.delay.assert_not_called()

        # Third run (simulating hourly execution) - should still NOT send notification
        tasks.create_reviews_if_strategy_is_after_proposal()

        # Verify still no new notification
        mock_notify.delay.assert_not_called()

    @mock.patch(
        "waldur_mastermind.proposal.utils.tasks.notify_user_about_proposal_state_update"
    )
    @mock.patch("waldur_mastermind.proposal.utils.get_available_reviewer")
    def test_after_round_task_no_duplicate_notifications(
        self, mock_get_reviewer, mock_notify
    ):
        """Test that create_reviews_if_strategy_is_after_round doesn't send duplicate notifications."""
        # Setup: Set round to use AFTER_ROUND strategy and make it active
        now = timezone.now()
        self.round.review_strategy = Round.ReviewStrategies.AFTER_ROUND
        self.round.start_time = now - timedelta(days=1)
        self.round.cutoff_time = now + timedelta(days=1)
        self.round.save()

        # Set call to ACTIVE state
        self.call.state = CallStates.ACTIVE
        self.call.save()

        # Set proposal to SUBMITTED state
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()

        # Mock get_available_reviewer to return empty list
        mock_get_reviewer.return_value = []

        # First run of the periodic task - should send notification
        tasks.create_reviews_if_strategy_is_after_round()

        # Verify notification was sent once
        self.assertEqual(mock_notify.delay.call_count, 1)
        mock_notify.delay.assert_called_with(
            self.proposal.uuid, ProposalStates.SUBMITTED, ProposalStates.IN_REVIEW
        )

        # Refresh proposal to get updated state
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)

        # Reset mock
        mock_notify.delay.reset_mock()

        # Second run of the periodic task - should NOT send notification
        tasks.create_reviews_if_strategy_is_after_round()

        # Verify no new notification was sent
        mock_notify.delay.assert_not_called()

        # Multiple subsequent runs (simulating hourly execution)
        for _ in range(5):
            tasks.create_reviews_if_strategy_is_after_round()

        # Verify still no duplicate notifications
        mock_notify.delay.assert_not_called()

    @mock.patch(
        "waldur_mastermind.proposal.utils.tasks.notify_user_about_proposal_state_update"
    )
    @mock.patch("waldur_mastermind.proposal.utils.get_available_reviewer")
    def test_multiple_proposals_each_notified_once(
        self, mock_get_reviewer, mock_notify
    ):
        """Test that multiple proposals each get notified exactly once."""
        # Create additional proposals
        proposal2 = proposal_factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        proposal3 = proposal_factories.ProposalFactory(
            round=self.round,
            state=ProposalStates.IN_REVIEW,  # Already in review
        )

        # Setup round for AFTER_PROPOSAL strategy
        self.round.review_strategy = Round.ReviewStrategies.AFTER_PROPOSAL
        self.round.save()

        # Set call to ACTIVE
        self.call.state = CallStates.ACTIVE
        self.call.save()

        # Set first proposal to SUBMITTED
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save()

        # Mock get_available_reviewer to return empty list
        mock_get_reviewer.return_value = []

        # Run the periodic task
        tasks.create_reviews_if_strategy_is_after_proposal()

        # Should have 2 notifications (for proposal1 and proposal2, not proposal3)
        self.assertEqual(mock_notify.delay.call_count, 2)

        # Verify the specific calls
        calls = mock_notify.delay.call_args_list
        proposal_uuids_notified = {call[0][0] for call in calls}
        self.assertIn(self.proposal.uuid, proposal_uuids_notified)
        self.assertIn(proposal2.uuid, proposal_uuids_notified)
        self.assertNotIn(proposal3.uuid, proposal_uuids_notified)

        # Reset mock
        mock_notify.delay.reset_mock()

        # Run task again - should send NO notifications
        tasks.create_reviews_if_strategy_is_after_proposal()
        mock_notify.delay.assert_not_called()
