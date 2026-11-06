"""Notification rules addressed to the teams of a proposal's offerings.

The hosting entities hear about the proposals that involve their offerings:
the ones requested until the award is decided, and the ones awarded after
it. Managers of other offerings in the call, and managers who are on the
applicant team, are never addressed.
"""

from contextlib import contextmanager
from unittest import mock

from django.core import mail
from django.db import transaction
from django.test import override_settings
from rest_framework import status

from waldur_core.core.models import Notification
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, OfferingRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import notification_rules, tasks, workflow_service
from waldur_mastermind.proposal.enums import (
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    ResultsPublication,
    RoundLifecycleStates,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.models import (
    CallWorkflowStepNotificationRule,
    Proposal,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_awarded_resources import (
    AwardedResourcesBase,
    _limit_offering,
)


@contextmanager
def mail_after_commit():
    """Deliver step mail once the block is over, as the worker does after the
    transaction commits: the test suite runs ``on_commit`` callbacks at once,
    before the allocation that the same transaction goes on to make."""
    with mock.patch.object(tasks.notify_workflow_step_event, "delay") as delay:
        yield
    for call in delay.call_args_list:
        tasks.notify_workflow_step_event(*call.args)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingTeamsRecipientTest(AwardedResourcesBase):
    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()

        # The requested offering's team, the team of an offering the award
        # may move the item to, and the team of an offering nobody involves.
        self.requested_team = structure_factories.UserFactory()
        self.offering.add_user(self.requested_team, OfferingRole.MANAGER)
        self.other_team = structure_factories.UserFactory()
        self.other_offering.add_user(self.other_team, OfferingRole.MANAGER)
        bystander_offering, bystander_plan = _limit_offering()
        factories.RequestedOfferingFactory(
            call=self.call, offering=bystander_offering, plan=bystander_plan
        )
        self.bystander = structure_factories.UserFactory()
        bystander_offering.add_user(self.bystander, OfferingRole.MANAGER)

        self.decision_step = self.call.workflow_steps.get(key="allocation_decision")
        self.rule = CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.decision_step,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.OFFERING_TEAMS,
        )
        mail.outbox = []

    def _recipients(self):
        return sorted(email for message in mail.outbox for email in message.to)

    def _move_award_to_other_offering(self):
        awarded = self._awarded()
        awarded.requested_offering = self.other_requested_offering
        awarded.save()

    def _decide_and_send(self, outcome=WorkflowStepOutcomes.APPROVED):
        with mail_after_commit():
            self._decide(outcome=outcome)

    def test_before_the_decision_the_requested_offerings_teams_are_addressed(self):
        self._start_allocation()
        self._move_award_to_other_offering()

        users = notification_rules.resolve_recipients(self.rule, self.proposal)

        self.assertEqual(list(users), [self.requested_team])

    def test_an_approved_decision_mails_the_awarded_offerings_teams(self):
        self._start_allocation()
        self._move_award_to_other_offering()

        self._decide_and_send()

        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        # The offering the item was moved away from has no part in it now; the
        # bystander never had one.
        self.assertEqual(self._recipients(), [self.other_team.email])

    def test_the_mail_names_the_granted_project(self):
        self._start_allocation()

        self._decide_and_send()

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn(self.proposal.project.name, body)
        self.assertIn(f"/projects/{self.proposal.project.uuid.hex}/", body)
        # The offering team's own view of the proposal, in its provider's
        # workspace.
        self.assertIn(
            f"/providers/{self.offering.customer.uuid.hex}"
            f"/call-proposals/{self.proposal.uuid.hex}/",
            body,
        )
        self.assertNotIn("call-management", body)

    def test_each_team_member_gets_a_private_copy(self):
        colleague = structure_factories.UserFactory()
        self.offering.add_user(colleague, OfferingRole.MANAGER)
        self._start_allocation()

        self._decide_and_send()

        self.assertEqual(
            sorted(tuple(message.to) for message in mail.outbox),
            sorted([(self.requested_team.email,), (colleague.email,)]),
        )

    def test_an_offering_manager_on_the_applicant_team_is_not_addressed(self):
        self.proposal.add_user(self.requested_team, ProposalRole.MEMBER)
        self._start_allocation()

        self._decide_and_send()

        self.assertEqual(mail.outbox, [])

    def test_a_declined_decision_does_not_fire_a_completed_rule(self):
        self._start_allocation()

        self._decide_and_send(outcome=WorkflowStepOutcomes.DECLINED)

        self.assertEqual(mail.outbox, [])

    def test_existing_recipients_are_unchanged(self):
        self.rule.recipient = NotificationRuleRecipients.CALL_MANAGERS
        self.rule.save()
        self._start_allocation()

        self._decide_and_send()

        self.assertEqual(self._recipients(), [self.call_manager.email])


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingTeamsHeldDecisionTest(OfferingTeamsRecipientTest):
    """A decision held for the round is told to the hosting entities only
    when the round publishes it."""

    def setUp(self):
        super().setUp()
        CallRole.MANAGER.add_permission(PermissionEnum.UPDATE_CALL)
        self.call.publish_results = ResultsPublication.WITH_ROUND
        self.call.save(update_fields=["publish_results"])
        self.round = self.proposal.round
        self.round.lifecycle_state = RoundLifecycleStates.EVALUATING
        self.round.save(update_fields=["lifecycle_state"])

    def _decide_and_send(self, outcome=WorkflowStepOutcomes.APPROVED):
        with mail_after_commit():
            self._decide(outcome=outcome)
        self.assertTrue(self.proposal.decision_held)
        self.assertEqual(mail.outbox, [])
        with mail_after_commit():
            with transaction.atomic():
                proposal = Proposal.objects.select_for_update().get(pk=self.proposal.pk)
                workflow_service.release_held_decision(proposal)
        self.proposal.refresh_from_db()


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingTeamsAwardResponseTest(AwardedResourcesBase):
    """The applicant accepting the award is what the hosting entities hear."""

    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        self.team = structure_factories.UserFactory()
        self.offering.add_user(self.team, OfferingRole.MANAGER)
        response_step = factories.CallWorkflowStepFactory(
            call=self.call, step="award_response", is_enabled=True
        )
        factories.ProposalWorkflowStepInstanceFactory(
            proposal=self.proposal,
            step="award_response",
            status=WorkflowStepInstanceStatuses.PENDING,
        )
        CallWorkflowStepNotificationRule.objects.create(
            workflow_step=response_step,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.OFFERING_TEAMS,
        )
        self._start_allocation()
        self._decide()
        self.assertEqual(self.proposal.workflow_step, "award_response")
        mail.outbox = []

    def _respond(self, outcome):
        instance = self.proposal.workflow_step_instances.get(step="award_response")
        with mail_after_commit():
            self.client.force_authenticate(self.applicant)
            response = self.client.post(
                factories.ProposalFactory.get_url(
                    self.proposal, action="complete_workflow_step"
                ),
                {"step_uuid": instance.uuid.hex, "outcome": outcome},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()

    def test_accepting_the_award_mails_the_hosting_entities_once(self):
        self._respond(WorkflowStepOutcomes.ACCEPTED)

        self.assertEqual(self.proposal.state, ProposalStates.ACCEPTED)
        self.assertEqual([message.to for message in mail.outbox], [[self.team.email]])
        self.assertIn(self.proposal.project.name, mail.outbox[0].body)

    def test_declining_the_award_mails_nobody_under_a_completed_rule(self):
        self._respond(WorkflowStepOutcomes.DECLINED)

        self.assertEqual(mail.outbox, [])
