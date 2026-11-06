"""Workflow-step mail tells each recipient no more of the step's outcome
than the proposal's workflow states show them."""

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import test

from waldur_core.core.models import Notification
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import (
    NotificationRuleRecipients,
    NotificationRuleTriggers,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_step_inputs import StepInputsTestMixin

REASON = "Consolidated: strong science, weak scaling plan."


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class WithheldFeedbackMailTest(StepInputsTestMixin, test.APITestCase):
    """The reason given on completing a report released to the applicant is
    the call team's; after the release also the step's holders'; never the
    other evaluators'."""

    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.reach_consolidated_step()
        self.step_instance = self.instance(self.consolidated_step)
        self.step_instance.outcome = "done"
        self.step_instance.outcome_reason = REASON
        self.step_instance.feedback_withheld = True
        self.step_instance.completed_at = timezone.now()
        self.step_instance.save()
        # Reviews the proposal, holds nothing of this step, no call role.
        self.other_reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(proposal=self.proposal, reviewer=self.other_reviewer)
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        for recipient, extra in (
            (NotificationRuleRecipients.CALL_MANAGERS, {}),
            (NotificationRuleRecipients.ASSIGNED_REVIEWERS, {}),
            (NotificationRuleRecipients.PROPOSAL_ROLE, {"proposal_role": self.role}),
        ):
            models.CallWorkflowStepNotificationRule.objects.create(
                workflow_step=self.consolidated_step,
                trigger=NotificationRuleTriggers.STEP_COMPLETED,
                recipient=recipient,
                **extra,
            )

    def send(self):
        mail.outbox = []
        tasks.notify_workflow_step_event(
            self.step_instance.uuid.hex, NotificationRuleTriggers.STEP_COMPLETED
        )

    def body_to(self, user):
        [body] = [m.body for m in mail.outbox if user.email in m.to]
        self.assertIn("has been completed", body)
        return body

    def test_before_the_release_only_the_call_team_reads_the_reason(self):
        self.send()

        self.assertIn(REASON, self.body_to(self.manager))
        self.assertNotIn(REASON, self.body_to(self.rapporteur_1))
        self.assertNotIn(REASON, self.body_to(self.other_reviewer))

    def test_after_the_release_the_step_holders_read_it_too(self):
        self.step_instance.released_at = timezone.now()
        self.step_instance.save(update_fields=["released_at"])

        self.send()

        self.assertIn(REASON, self.body_to(self.manager))
        self.assertIn(REASON, self.body_to(self.rapporteur_1))
        self.assertNotIn(REASON, self.body_to(self.other_reviewer))


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class HiddenStepMailTest(StepInputsTestMixin, test.APITestCase):
    """A step the call keeps from the applicant: someone on the applicant
    team who also manages the call reads it from the applicant's side, in
    mail as on the proposal's workflow states."""

    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.reach_consolidated_step()
        self.consolidated_step.applicant_visible = False
        self.consolidated_step.save(update_fields=["applicant_visible"])
        self.step_instance = self.instance(self.consolidated_step)
        self.step_instance.outcome = "done"
        self.step_instance.outcome_reason = REASON
        self.step_instance.feedback_withheld = False
        self.step_instance.completed_at = timezone.now()
        self.step_instance.save()
        self.applicant = self.fixture.proposal_creator
        self.call.add_user(self.applicant, CallRole.MANAGER)
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.consolidated_step,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
        )

    def test_the_outcome_is_the_call_teams_alone(self):
        self.assertEqual(
            tasks._step_outcome_for(self.manager, self.step_instance),
            ("done", REASON),
        )
        self.assertEqual(
            tasks._step_outcome_for(self.applicant, self.step_instance), (None, "")
        )

    def test_mail_to_a_dual_role_applicant_carries_no_outcome(self):
        mail.outbox = []
        tasks.notify_workflow_step_event(
            self.step_instance.uuid.hex, NotificationRuleTriggers.STEP_COMPLETED
        )
        manager_bodies = [m.body for m in mail.outbox if self.manager.email in m.to]
        self.assertTrue(manager_bodies)
        self.assertIn(REASON, manager_bodies[0])
        for message in mail.outbox:
            if self.applicant.email in message.to:
                self.assertNotIn(REASON, message.body)
                self.assertNotIn("Outcome", message.body)
