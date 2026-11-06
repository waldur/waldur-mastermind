"""Proposal mail tells its recipients no more than the API shows them."""

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import test

from waldur_core.core.models import Notification
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import (
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories, fixtures

REASON = "The panel found the method unsound"


def bodies_to(email):
    return [m.body for m in mail.outbox if email in m.to]


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class ReviewAssignedMailTest(test.APITestCase):
    def setUp(self):
        Notification.objects.update_or_create(
            key="proposal.review_assigned", defaults={"enabled": True}
        )
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.creator_name = self.proposal.created_by.full_name
        self.review = factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.fixture.reviewer_1
        )
        mail.outbox = []

    def send(self):
        tasks.notify_reviewer_about_assignment(self.review.uuid.hex)
        [body] = bodies_to(self.fixture.reviewer_1.email)
        return body

    def test_name_concealed_by_the_call_is_left_out(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.fixture.call, expose_full_name=False
        )

        body = self.send()

        self.assertNotIn(self.creator_name, body)
        self.assertNotIn("Submitted by", body)
        self.assertIn(self.proposal.name, body)

    def test_name_exposed_by_the_call_is_included(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.fixture.call, expose_full_name=True
        )

        self.assertIn(f"Submitted by: {self.creator_name}", self.send())


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class StepEventOutcomeMailTest(test.APITestCase):
    """Outcome and reason go to those the proposal's workflow states show
    them to; everyone else the rule addresses gets the event without them."""

    def setUp(self):
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        # Review content is the call team's alone.
        self.call.reviews_visible_to_submitters = False
        self.call.save(update_fields=["reviews_visible_to_submitters"])
        self.manager = self.fixture.call_manager
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.save()
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        # Assigned to review, without any role on the call.
        self.assigned = structure_factories.UserFactory()
        factories.ReviewFactory(proposal=self.proposal, reviewer=self.assigned)

    def complete(self, step, outcome, contributor=None):
        call_step = self.call.workflow_steps.get(key=step)
        for recipient in (
            NotificationRuleRecipients.CALL_MANAGERS,
            NotificationRuleRecipients.ASSIGNED_REVIEWERS,
        ):
            models.CallWorkflowStepNotificationRule.objects.create(
                workflow_step=call_step,
                trigger=NotificationRuleTriggers.STEP_COMPLETED,
                recipient=recipient,
            )
        instance = models.ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step=step,
            status=WorkflowStepInstanceStatuses.COMPLETED,
            started_at=timezone.now(),
            completed_at=timezone.now(),
            outcome=outcome,
            outcome_reason=REASON,
        )
        if contributor is not None:
            # Their work on the step: a reviewer reads the steps they take
            # part in, as the proposal's workflow states show them.
            models.StepContribution.objects.create(
                proposal_step=instance, contributor=contributor
            )
        mail.outbox = []
        tasks.notify_workflow_step_event(
            instance.uuid.hex, NotificationRuleTriggers.STEP_COMPLETED
        )

    def assert_reads_outcome(self, user, reads):
        [body] = bodies_to(user.email)
        self.assertIn("has been completed", body)
        if reads:
            self.assertIn(REASON, body)
        else:
            self.assertNotIn(REASON, body)
            self.assertNotIn("Outcome:", body)

    def test_review_verdict_reaches_the_call_team_only(self):
        self.complete("expert_review", "approved")

        self.assert_reads_outcome(self.manager, True)
        self.assert_reads_outcome(self.assigned, False)

    def test_review_verdict_reaches_reviewers_when_the_call_shows_reviews(self):
        self.call.reviews_visible_to_submitters = True
        self.call.save(update_fields=["reviews_visible_to_submitters"])

        self.complete("expert_review", "approved", contributor=self.assigned)

        self.assert_reads_outcome(self.assigned, True)

    def test_held_decision_reaches_only_those_who_see_held_decisions(self):
        self.proposal.decision_held = True
        self.proposal.save(update_fields=["decision_held"])

        self.complete("allocation_decision", "rejected")

        self.assert_reads_outcome(self.manager, True)
        self.assert_reads_outcome(self.assigned, False)

    def test_other_steps_reach_whoever_takes_part_in_them(self):
        self.complete("technical_assessment", "feasible")

        self.assert_reads_outcome(self.manager, True)
        # A reviewer reads the outcome of a step they take no part in no more
        # in mail than through the workflow states.
        self.assert_reads_outcome(self.assigned, False)
