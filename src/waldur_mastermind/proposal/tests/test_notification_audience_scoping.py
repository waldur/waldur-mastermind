"""Workflow mail reaches only the people the evaluation involves: the
reviewers holding work on the proposal, never the applicant team on an
evaluator's audience, never someone a conflict of interest stands against,
and the applicant only about the steps they are told of."""

from unittest import mock

from django.core import mail
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import override_settings
from django.utils import timezone
from rest_framework import test

from waldur_core.core.models import Notification
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import call_templates, models, tasks
from waldur_mastermind.proposal.enums import (
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ResponsibleRoles,
    StepKinds,
    WorkflowPhases,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_step_inputs import StepInputsTestMixin


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class StepMailAudienceTestBase(StepInputsTestMixin, test.APITestCase):
    trigger = NotificationRuleTriggers.STEP_COMPLETED

    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.reach_consolidated_step()
        self.step_instance = self.instance(self.consolidated_step)
        self.step_instance.outcome = "done"
        self.step_instance.completed_at = timezone.now()
        self.step_instance.deadline = timezone.now() + timezone.timedelta(days=1)
        self.step_instance.save()
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()

    def rule(self, recipient, trigger=None):
        return models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step_id=self.consolidated_step.pk,
            trigger=trigger or self.trigger,
            recipient=recipient,
        )

    def send(self, trigger=None):
        mail.outbox = []
        tasks.notify_workflow_step_event(
            self.step_instance.uuid.hex, trigger or self.trigger
        )
        return [address for message in mail.outbox for address in message.to]

    def conflict(self, user):
        profile = factories.ReviewerProfileFactory(user=user)
        factories.ConflictOfInterestFactory(reviewer=profile, proposal=self.proposal)


class ReviewerCompleterMailTest(StepMailAudienceTestBase):
    """A step the reviewers complete is mailed to the reviewers holding work
    on the proposal, not to the call's whole pool."""

    trigger = NotificationRuleTriggers.STEP_STARTED

    def setUp(self):
        super().setUp()
        models.CallWorkflowStep.objects.filter(pk=self.consolidated_step.pk).update(
            completer=ResponsibleRoles.REVIEWER
        )
        self.rule(NotificationRuleRecipients.RESPONSIBLE_ROLE)

    def test_pool_reviewer_without_work_on_the_proposal_is_not_mailed(self):
        outsider = structure_factories.UserFactory()
        self.call.add_user(outsider, CallRole.REVIEWER)

        self.assertNotIn(outsider.email, self.send())

    def test_reviewer_with_a_live_review_is_mailed(self):
        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        factories.ReviewFactory(proposal=self.proposal, reviewer=reviewer)

        self.assertIn(reviewer.email, self.send())


class ReviewerAudienceMailTest(StepMailAudienceTestBase):
    def test_reviewer_of_a_rejected_review_is_not_mailed(self):
        reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=reviewer,
            state=models.Review.States.REJECTED,
        )
        self.rule(NotificationRuleRecipients.ASSIGNED_REVIEWERS)

        self.assertNotIn(reviewer.email, self.send())

    def test_conflicted_reviewer_of_a_live_review_is_not_mailed(self):
        reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(proposal=self.proposal, reviewer=reviewer)
        self.conflict(reviewer)
        self.rule(NotificationRuleRecipients.ASSIGNED_REVIEWERS)

        self.assertNotIn(reviewer.email, self.send())

    def test_reviewer_of_a_live_review_is_mailed(self):
        reviewer = structure_factories.UserFactory()
        factories.ReviewFactory(proposal=self.proposal, reviewer=reviewer)
        self.rule(NotificationRuleRecipients.ASSIGNED_REVIEWERS)

        self.assertIn(reviewer.email, self.send())

    def test_conflicted_pool_reviewer_is_not_mailed(self):
        conflicted = structure_factories.UserFactory()
        self.call.add_user(conflicted, CallRole.REVIEWER)
        self.conflict(conflicted)
        self.rule(NotificationRuleRecipients.ALL_POOL_REVIEWERS)

        recipients = self.send()

        self.assertNotIn(conflicted.email, recipients)
        self.assertFalse(
            [
                message
                for message in mail.outbox
                if conflicted.email in message.to and self.proposal.name in message.body
            ]
        )

    def test_conflicted_call_manager_is_still_mailed(self):
        self.conflict(self.manager)
        self.rule(NotificationRuleRecipients.CALL_MANAGERS)

        self.assertIn(self.manager.email, self.send())


class ApplicantTeamOffEvaluatorAudiencesTest(StepMailAudienceTestBase):
    """Someone on the applicant team hears of their own proposal's
    evaluation as an applicant, whatever role they also hold on the call."""

    def setUp(self):
        super().setUp()
        self.consolidated_step.applicant_visible = False
        self.consolidated_step.save(update_fields=["applicant_visible"])

    def test_applicant_who_manages_the_call_is_not_mailed(self):
        self.call.add_user(self.applicant, CallRole.MANAGER)
        self.rule(NotificationRuleRecipients.CALL_MANAGERS)

        recipients = self.send()

        self.assertNotIn(self.applicant.email, recipients)
        self.assertIn(self.manager.email, recipients)

    def test_applicant_in_the_reviewer_pool_is_not_mailed(self):
        self.call.add_user(self.applicant, CallRole.REVIEWER)
        self.rule(NotificationRuleRecipients.ALL_POOL_REVIEWERS)

        self.assertNotIn(self.applicant.email, self.send())


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class ReviewMailsSkipApplicantTeamTest(StepInputsTestMixin, test.APITestCase):
    """Reviewer names and scores reach the call's managers, never one of
    them who is on the proposal's applicant team."""

    def setUp(self):
        super().setUp()
        self.call.add_user(self.applicant, CallRole.MANAGER)
        self.review = factories.ReviewFactory(
            proposal=self.proposal,
            reviewer=self.reviewer,
            state=models.Review.States.SUBMITTED,
            summary_score=4,
        )

    def test_reviews_complete_mail_skips_the_applicant_team(self):
        [message] = tasks.reviews_completed_mails(self.proposal)

        self.assertIn(self.manager.email, message.recipients)
        self.assertNotIn(self.applicant.email, message.recipients)

    @mock.patch("waldur_mastermind.proposal.tasks.core_utils.broadcast_mail")
    def test_new_review_mail_skips_the_applicant_team(self, broadcast_mail):
        tasks.notify_call_managers_about_new_review(self.review.uuid.hex)

        recipients = broadcast_mail.call_args.args[3]
        self.assertIn(self.manager.email, recipients)
        self.assertNotIn(self.applicant.email, recipients)

    @mock.patch("waldur_mastermind.proposal.tasks.core_utils.broadcast_mail")
    def test_rejected_review_mail_skips_the_applicant_team(self, broadcast_mail):
        tasks.notify_call_managers_about_rejected_review(self.review.uuid.hex)

        recipients = broadcast_mail.call_args.args[3]
        self.assertIn(self.manager.email, recipients)
        self.assertNotIn(self.applicant.email, recipients)


class ApplicantNeverToldOfInternalStepsTest(StepMailAudienceTestBase):
    """The applicant is never mailed about an internal step, whatever rule
    the step carries."""

    def setUp(self):
        super().setUp()
        self.assertTrue(models.is_internal_step(self.consolidated_step))

    def test_rule_made_before_the_step_turned_internal_sends_nothing(self):
        # Written past validation, as an import or an older step would.
        models.CallWorkflowStepNotificationRule.objects.bulk_create(
            [
                models.CallWorkflowStepNotificationRule(
                    workflow_step_id=self.consolidated_step.pk,
                    trigger=self.trigger,
                    recipient=NotificationRuleRecipients.APPLICANT,
                )
            ]
        )

        self.assertNotIn(self.applicant.email, self.send())

    def test_model_validation_refuses_the_rule(self):
        rule = models.CallWorkflowStepNotificationRule(
            workflow_step=self.consolidated_step,
            trigger=self.trigger,
            recipient=NotificationRuleRecipients.APPLICANT,
        )

        with self.assertRaises(DjangoValidationError) as context:
            rule.clean()
        self.assertIn("recipient", context.exception.message_dict)


class DecisionsTheApplicantHearsOfTest(test.APITestCase):
    """A decision whose details the applicant is shown, or one closing a run
    they asked for after the award, is a verdict on them they are told of."""

    def step(self, **fields):
        call = factories.CallFactory()
        defaults = dict(
            call=call,
            key="decision",
            name="Decision",
            kind=StepKinds.DECISION,
            completer=ResponsibleRoles.CALL_MANAGER,
            order=50,
        )
        defaults.update(fields)
        return models.CallWorkflowStep(**defaults)

    def test_internal_decision(self):
        self.assertTrue(models.is_internal_step(self.step()))

    def test_decision_shown_to_the_applicant(self):
        self.assertFalse(models.is_internal_step(self.step(applicant_visible=True)))

    def test_decision_closing_a_post_award_run(self):
        for phase in (WorkflowPhases.EXTENSION, WorkflowPhases.CLOSING):
            self.assertFalse(models.is_internal_step(self.step(phase=phase)))

    def test_shipped_templates_tell_the_applicant_of_no_internal_step(self):
        for name in call_templates.names():
            document = call_templates.load(name)
            for step in document.get("workflow_steps", []):
                view = self.step(
                    key=step["key"],
                    kind=step["kind"],
                    completer=step.get("completer"),
                    applicant_visible=bool(step.get("applicant_visible")),
                    phase=step.get("phase") or WorkflowPhases.EVALUATION,
                    is_allocation_decision=bool(step.get("is_allocation_decision")),
                )
                for rule in step.get("notification_rules", []):
                    if models.is_applicant_recipient(rule["recipient"], view):
                        self.assertFalse(
                            models.is_internal_step(view),
                            f"{name}: {step['key']} {rule}",
                        )
