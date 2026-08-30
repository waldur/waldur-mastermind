"""Call-level workflow notification rules: config API, seeding, and delivery."""

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.models import Notification
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import handlers, tasks, workflow_service
from waldur_mastermind.proposal.enums import (
    CallStates,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.models import (
    CallWorkflowStep,
    CallWorkflowStepNotificationRule,
    ProposalWorkflowStepInstance,
)
from waldur_mastermind.proposal.tests import fixtures

LIST_URL = "/api/call-workflow-step-notification-rules/"


def rule_url(rule):
    return f"{LIST_URL}{rule.uuid.hex}/"


class NotificationRuleSeedTest(test.APITestCase):
    def test_new_call_gets_wal_9502_defaults(self):
        call = fixtures.ProposalFixture().call
        admin_step = call.workflow_steps.get(step="administrative_check")
        rules = {
            (r.trigger, r.recipient, r.days_before)
            for r in admin_step.notification_rules.all()
        }
        self.assertEqual(
            rules,
            {
                ("deadline_approaching", "responsible_role", 1),
                ("deadline_approaching", "call_managers", 1),
                ("step_expired", "responsible_role", None),
                ("step_expired", "call_managers", None),
            },
        )
        # Applicants are not told that evaluation is progressing unless a
        # manager opts in; the seed never addresses them on the decision step.
        decision_step = call.workflow_steps.get(step="allocation_decision")
        self.assertFalse(
            decision_step.notification_rules.filter(recipient="applicant").exists()
        )
        panel_step = call.workflow_steps.get(step="panel_review")
        self.assertEqual(
            set(
                panel_step.notification_rules.filter(
                    recipient="panel_chair"
                ).values_list("trigger", flat=True)
            ),
            {"deadline_approaching", "step_expired"},
        )
        # Internal steps never address the applicant.
        self.assertFalse(
            CallWorkflowStepNotificationRule.objects.filter(
                workflow_step__call=call,
                workflow_step__step__in=["administrative_check", "expert_review"],
                recipient="applicant",
            ).exists()
        )

    def test_seed_is_idempotent(self):
        call = fixtures.ProposalFixture().call
        step = call.workflow_steps.get(step="administrative_check")
        before = step.notification_rules.count()
        handlers.seed_notification_rules(step)
        self.assertEqual(step.notification_rules.count(), before)


class NotificationRuleApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.manager = self.fixture.call_manager
        self.step = self.call.workflow_steps.get(step="allocation_decision")
        self.internal_step = self.call.workflow_steps.get(step="expert_review")

    def _create(self, user, **overrides):
        self.client.force_authenticate(user)
        payload = {
            "workflow_step": self.step.uuid.hex,
            "trigger": NotificationRuleTriggers.STEP_COMPLETED,
            "recipient": NotificationRuleRecipients.CALL_MANAGERS,
        }
        payload.update(overrides)
        return self.client.post(LIST_URL, payload)

    def test_call_manager_can_create_rule(self):
        response = self._create(self.manager)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["step"], "allocation_decision")
        self.assertEqual(response.data["call_uuid"], self.call.uuid.hex)

    def test_reviewer_cannot_create_rule(self):
        response = self._create(self.fixture.reviewer_1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unrelated_user_cannot_list_rules(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(LIST_URL, {"call_uuid": self.call.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_reviewer_can_list_rules(self):
        self.client.force_authenticate(self.fixture.reviewer_1)
        response = self.client.get(LIST_URL, {"call_uuid": self.call.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)

    def test_deadline_approaching_requires_days_before(self):
        # assigned_reviewers is not among the seeded defaults, so the only
        # objection can be the missing lead time.
        response = self._create(
            self.manager,
            trigger=NotificationRuleTriggers.DEADLINE_APPROACHING,
            recipient=NotificationRuleRecipients.ASSIGNED_REVIEWERS,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("days_before", response.data)

    def test_days_before_rejected_on_other_triggers(self):
        response = self._create(self.manager, days_before=3)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("days_before", response.data)

    def test_applicant_rule_rejected_on_internal_step(self):
        response = self._create(
            self.manager,
            workflow_step=self.internal_step.uuid.hex,
            recipient=NotificationRuleRecipients.APPLICANT,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("recipient", response.data)

    def test_duplicate_rule_rejected(self):
        self.assertEqual(self._create(self.manager).status_code, 201)
        response = self._create(self.manager)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manager_can_toggle_and_delete(self):
        rule = self.step.notification_rules.first()
        self.client.force_authenticate(self.manager)
        response = self.client.patch(rule_url(rule), {"is_enabled": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rule.refresh_from_db()
        self.assertFalse(rule.is_enabled)
        response = self.client.delete(rule_url(rule))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_archived_call_is_read_only(self):
        self.call.state = CallStates.ARCHIVED
        self.call.save()
        rule = self.step.notification_rules.first()
        self.client.force_authenticate(self.manager)
        self.assertEqual(
            self.client.patch(rule_url(rule), {"is_enabled": False}).status_code,
            status.HTTP_409_CONFLICT,
        )
        self.assertEqual(self._create(self.manager).status_code, 400)

    def test_rules_embedded_in_workflow_step(self):
        self.client.force_authenticate(self.manager)
        url = f"/api/proposal-protected-calls/{self.call.uuid.hex}/workflow_steps/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        decision = next(s for s in response.data if s["step"] == "allocation_decision")
        self.assertTrue(
            any(r["trigger"] == "step_expired" for r in decision["notification_rules"])
        )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class NotificationRuleDeliveryTest(test.APITestCase):
    """Rules fire from the workflow engine. Dispatch is deferred to
    ``transaction.on_commit``, so every trigger runs under
    ``captureOnCommitCallbacks(execute=True)``."""

    def setUp(self):
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.manager = self.fixture.call_manager
        self.proposal = self.fixture.proposal
        self.proposal.state = ProposalStates.IN_REVIEW
        self.proposal.workflow_step = "administrative_check"
        self.proposal.save()
        CallWorkflowStep.objects.filter(call=self.call).update(is_enabled=True)
        self.admin_step = self.call.workflow_steps.get(step="administrative_check")
        self.admin_step.duration_in_days = 5
        self.admin_step.save()
        # Only the rules under test should be live.
        CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        self.instance = ProposalWorkflowStepInstance.objects.create(
            proposal=self.proposal,
            step="administrative_check",
            status=WorkflowStepInstanceStatuses.ACTIVE,
            started_at=timezone.now(),
            deadline=timezone.now() + timedelta(days=5),
        )
        for step in (
            "technical_assessment",
            "expert_review",
            "panel_review",
            "allocation_decision",
        ):
            ProposalWorkflowStepInstance.objects.create(
                proposal=self.proposal,
                step=step,
                status=WorkflowStepInstanceStatuses.PENDING,
            )
        mail.outbox = []

    def _rule(self, trigger, recipient, step=None, **kwargs):
        return CallWorkflowStepNotificationRule.objects.create(
            workflow_step=step or self.admin_step,
            trigger=trigger,
            recipient=recipient,
            **kwargs,
        )

    def _complete(self):
        with self.captureOnCommitCallbacks(execute=True):
            workflow_service.complete_step(
                self.proposal,
                self.instance,
                WorkflowStepOutcomes.ELIGIBLE,
                "",
                self.manager,
            )

    def test_no_rule_no_mail(self):
        self._complete()
        self.assertEqual(len(mail.outbox), 0)

    def test_disabled_rule_no_mail(self):
        self._rule("step_completed", "call_managers", is_enabled=False)
        self._complete()
        self.assertEqual(len(mail.outbox), 0)

    def test_step_completed_mails_call_managers(self):
        self._rule("step_completed", "call_managers")
        self._complete()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.manager.email])
        self.assertIn("Administrative check", mail.outbox[0].subject)
        self.assertIn("eligible", mail.outbox[0].body)

    def test_step_started_fires_for_next_step(self):
        tech = self.call.workflow_steps.get(step="technical_assessment")
        self._rule("step_started", "call_managers", step=tech)
        self._complete()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Technical assessment", mail.outbox[0].subject)
        self.assertIn("has started", mail.outbox[0].subject)

    def test_applicant_mail_is_status_only(self):
        decision = self.call.workflow_steps.get(step="allocation_decision")
        self._rule("step_rejected", "applicant", step=decision)
        # Move the workflow to the decision step.
        self.instance.status = WorkflowStepInstanceStatuses.COMPLETED
        self.instance.save()
        decision_instance = self.proposal.workflow_step_instances.get(
            step="allocation_decision"
        )
        decision_instance.status = WorkflowStepInstanceStatuses.ACTIVE
        decision_instance.save()
        with self.captureOnCommitCallbacks(execute=True):
            workflow_service.reject_at_step(
                self.proposal,
                decision_instance,
                "Budget exhausted",
                self.manager,
                internal_notes="do not fund",
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.proposal.created_by.email])
        self.assertIn("Dear applicant", mail.outbox[0].body)
        self.assertNotIn("Budget exhausted", mail.outbox[0].body)
        self.assertNotIn("do not fund", mail.outbox[0].body)
        self.assertIn("/proposals/", mail.outbox[0].body)
        self.assertNotIn("call-management", mail.outbox[0].body)

    def test_manager_and_responsible_role_get_one_copy_each(self):
        self._rule("step_expired", "call_managers")
        self._rule("step_expired", "responsible_role")  # admin check → call manager
        self.instance.deadline = timezone.now() - timedelta(hours=1)
        self.instance.save()
        with self.captureOnCommitCallbacks(execute=True):
            tasks.mark_expired_workflow_steps()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("has expired", mail.outbox[0].subject)

    def test_unsubscribed_user_not_mailed(self):
        self._rule("step_completed", "call_managers")
        self.manager.notifications_enabled = False
        self.manager.save()
        self._complete()
        self.assertEqual(len(mail.outbox), 0)

    def test_deadline_reminder_fires_once_on_lead_day(self):
        self._rule("deadline_approaching", "call_managers", days_before=2)
        self.instance.deadline = timezone.now() + timedelta(days=2, hours=1)
        self.instance.save()
        tasks.send_workflow_step_deadline_reminders()
        tasks.send_workflow_step_deadline_reminders()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("due in 2 days", mail.outbox[0].subject)
        self.instance.refresh_from_db()
        self.assertIn("deadline_approaching:2", self.instance.sent_notifications)

    def test_deadline_reminder_skips_other_days(self):
        self._rule("deadline_approaching", "call_managers", days_before=1)
        self.instance.deadline = timezone.now() + timedelta(days=4)
        self.instance.save()
        tasks.send_workflow_step_deadline_reminders()
        self.assertEqual(len(mail.outbox), 0)

    def test_assigned_reviewers_recipient(self):
        expert = self.call.workflow_steps.get(step="expert_review")
        self._rule("step_started", "assigned_reviewers", step=expert)
        from waldur_mastermind.proposal.tests import factories

        factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.fixture.reviewer_1
        )
        # Skip straight to expert review.
        self.proposal.workflow_step_instances.filter(
            step="technical_assessment"
        ).update(status=WorkflowStepInstanceStatuses.SKIPPED)
        self._complete()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.fixture.reviewer_1.email])

    @mock.patch("waldur_mastermind.proposal.tasks.notify_workflow_step_event.delay")
    def test_dispatch_is_deferred_to_commit(self, delay):
        self._rule("step_completed", "call_managers")
        self._complete()
        delay.assert_called_once_with(self.instance.uuid.hex, "step_completed")

    def test_panel_chair_recipient(self):
        panel = self.call.workflow_steps.get(step="panel_review")
        self._rule("step_started", "panel_chair", step=panel)
        chair = self.fixture.panel_member
        self.call.panel_chair = chair
        self.call.save()
        self.proposal.workflow_step_instances.filter(
            step__in=["technical_assessment", "expert_review"]
        ).update(status=WorkflowStepInstanceStatuses.SKIPPED)
        self._complete()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [chair.email])

    def test_panel_chair_unset_is_inert(self):
        panel = self.call.workflow_steps.get(step="panel_review")
        self._rule("step_started", "panel_chair", step=panel)
        self.proposal.workflow_step_instances.filter(
            step__in=["technical_assessment", "expert_review"]
        ).update(status=WorkflowStepInstanceStatuses.SKIPPED)
        self._complete()
        self.assertEqual(len(mail.outbox), 0)

    def test_negative_outcome_fires_step_rejected(self):
        self._rule("step_rejected", "call_managers")
        self._rule("step_completed", "assigned_reviewers")
        with self.captureOnCommitCallbacks(execute=True):
            workflow_service.complete_step(
                self.proposal,
                self.instance,
                WorkflowStepOutcomes.INELIGIBLE,
                "Out of scope",
                self.manager,
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("was rejected", mail.outbox[0].subject)
        self.assertIn("Out of scope", mail.outbox[0].body)
