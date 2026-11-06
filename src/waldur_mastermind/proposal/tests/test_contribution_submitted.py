"""The ``contribution_submitted`` notification rule: one mail per contribution
handed in -- an offering's assessment, a review, a rapporteur's report --
never naming who handed it in."""

from django.core import mail
from django.test import override_settings
from rest_framework import status

from waldur_core.core.models import Notification
from waldur_mastermind.proposal import workflow_description
from waldur_mastermind.proposal.enums import (
    ContributionOutcomes,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.models import (
    CallWorkflowStep,
    CallWorkflowStepNotificationRule,
    Review,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_conditional_and_parallel_steps import (
    WorkflowBase,
)
from waldur_mastermind.proposal.tests.test_notification_rules import (
    LIST_URL,
    NotificationRuleDeliveryTestBase,
)

SUBMITTED = NotificationRuleTriggers.CONTRIBUTION_SUBMITTED


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class PerOfferingContributionTest(WorkflowBase):
    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.technical,
            trigger=SUBMITTED,
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
        )
        self._start()
        mail.outbox = []

    def _submit(self, requested, manager):
        with self.captureOnCommitCallbacks(execute=True):
            self._assess(
                "technical_assessment",
                requested,
                manager,
                ContributionOutcomes.ACCEPTED,
            )

    def test_each_contribution_is_announced_with_the_count(self):
        self._submit(self.requested_a, self.manager_a)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.call_manager.email])
        self.assertIn("was submitted", message.subject)
        self.assertIn("1 of 2 submitted so far", message.body)
        # Nobody is named.
        for name in (self.manager_a.full_name, self.manager_a.email):
            self.assertNotIn(name, message.body)

        self._submit(self.requested_b, self.manager_b)
        bodies = [m.body for m in mail.outbox if "was submitted" in m.subject]
        self.assertEqual(len(bodies), 2)
        self.assertIn("2 of 2 submitted so far", bodies[1])

    def test_without_a_rule_nothing_is_sent(self):
        CallWorkflowStepNotificationRule.objects.filter(trigger=SUBMITTED).delete()
        self._submit(self.requested_a, self.manager_a)
        self.assertEqual(mail.outbox, [])

    def test_the_applicant_hears_nothing_of_a_contribution(self):
        # Shown to the applicant or not, the step is the evaluation's own: a
        # rule addressing the applicant sends them no mail, so neither the
        # counts nor who contributed can reach them.
        self.technical.applicant_visible = True
        self.technical.save()
        CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.technical,
            trigger=SUBMITTED,
            recipient=NotificationRuleRecipients.APPLICANT,
        )
        self._submit(self.requested_a, self.manager_a)
        applicant = self.proposal.created_by.email
        self.assertFalse(
            [m for m in mail.outbox if applicant in m.to or applicant in m.cc]
        )


class ReviewContributionTest(NotificationRuleDeliveryTestBase):
    def setUp(self):
        super().setUp()
        self.review_step = self.call.workflow_steps.get(key="expert_review")
        self.review_step.blind_review = True
        self.review_step.save()
        self.instance.status = WorkflowStepInstanceStatuses.COMPLETED
        self.instance.save()
        self.review_instance = self.proposal.workflow_step_instances.get(
            step="expert_review"
        )
        self.review_instance.status = WorkflowStepInstanceStatuses.ACTIVE
        self.review_instance.save()
        self._rule(SUBMITTED, "call_managers", step=self.review_step)

    def test_a_submitted_review_is_announced_once(self):
        review = factories.ReviewFactory(proposal=self.proposal)
        self.assertEqual(mail.outbox, [])
        with self.captureOnCommitCallbacks(execute=True):
            review.state = Review.States.SUBMITTED
            review.save()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("1 of 1 submitted so far", message.body)
        self.assertNotIn(review.reviewer.full_name, message.body)
        # Saved again, it is not announced again.
        with self.captureOnCommitCallbacks(execute=True):
            review.summary_public_comment = "Edited"
            review.save()
        self.assertEqual(len(mail.outbox), 1)


class ContributionRuleConfigurationTest(NotificationRuleDeliveryTestBase):
    def test_the_applicant_is_never_told_of_an_internal_step(self):
        self.client.force_authenticate(self.manager)
        review_step = self.call.workflow_steps.get(key="expert_review")
        response = self.client.post(
            LIST_URL,
            {
                "workflow_step": review_step.uuid.hex,
                "trigger": SUBMITTED,
                "recipient": NotificationRuleRecipients.APPLICANT,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_rule_on_a_step_without_contributors_is_flagged(self):
        decision = CallWorkflowStep.objects.get(
            call=self.call, key="allocation_decision"
        )
        self._rule(SUBMITTED, "call_managers", step=decision)
        description = workflow_description.describe_call(self.call)
        flags = [(f["code"], f["step"]) for f in description["notifications"]["flags"]]
        self.assertIn(
            ("contribution_rule_without_contributions", "allocation_decision"), flags
        )
        self._rule(
            SUBMITTED,
            "call_managers",
            step=self.call.workflow_steps.get(key="expert_review"),
        )
        description = workflow_description.describe_call(self.call)
        flags = [(f["code"], f["step"]) for f in description["notifications"]["flags"]]
        self.assertNotIn(
            ("contribution_rule_without_contributions", "expert_review"), flags
        )
