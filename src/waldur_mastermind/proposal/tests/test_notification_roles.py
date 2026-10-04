"""Which proposal roles an applicant-audience step notification reaches.

A rule addressed to the applicant names the proposal roles to notify; without
a selection it reaches the creator and the whole proposal team. Every resolved
recipient is addressed in one message.
"""

from django.core import mail
from rest_framework import status

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CallRole, ProjectRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_transfer,
    notification_rules,
    workflow_service,
)
from waldur_mastermind.proposal.enums import (
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ResponsibleRoles,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests.test_notification_rules import (
    LIST_URL,
    NotificationRuleDeliveryTestBase,
    rule_url,
)


class ApplicantRolesMixin:
    def setUp(self):
        super().setUp()
        self.applicant = self.proposal.created_by
        self.applicant.email = "applicant@example.com"
        self.applicant.save()
        self.pi = structure_factories.UserFactory(email="pi@example.com")
        self.proposal.add_user(self.pi, ProposalRole.MANAGER)
        self.editor = structure_factories.UserFactory(email="editor@example.com")
        self.proposal.add_user(self.editor, ProposalRole.ADMIN)
        self.member = structure_factories.UserFactory(email="member@example.com")
        self.proposal.add_user(self.member, ProposalRole.MEMBER)
        self.decision = self.call.workflow_steps.get(step="allocation_decision")


class NotifiedRolesApiTest(ApplicantRolesMixin, NotificationRuleDeliveryTestBase):
    def _create(self, **overrides):
        self.client.force_authenticate(self.manager)
        payload = {
            "workflow_step": self.decision.uuid.hex,
            "trigger": NotificationRuleTriggers.STEP_REJECTED,
            "recipient": NotificationRuleRecipients.APPLICANT,
        }
        payload.update(overrides)
        return self.client.post(LIST_URL, payload, format="json")

    def test_rule_picks_proposal_roles(self):
        response = self._create(
            notified_proposal_roles=[RoleEnum.PROPOSAL_MANAGER, RoleEnum.PROPOSAL_ADMIN]
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            sorted(response.data["notified_proposal_roles"]),
            [RoleEnum.PROPOSAL_ADMIN, RoleEnum.PROPOSAL_MANAGER],
        )

    def test_selection_defaults_to_empty(self):
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["notified_proposal_roles"], [])

    def test_only_proposal_roles_may_be_picked(self):
        ProjectRole.MANAGER
        response = self._create(notified_proposal_roles=[RoleEnum.PROJECT_MANAGER])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("notified_proposal_roles", response.data)

    def test_roles_only_apply_to_an_applicant_audience(self):
        response = self._create(
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
            notified_proposal_roles=[RoleEnum.PROPOSAL_MANAGER],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("notified_proposal_roles", response.data)

    def test_selection_can_be_changed(self):
        rule = self._rule("step_rejected", "applicant", step=self.decision)
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            rule_url(rule),
            {"notified_proposal_roles": [RoleEnum.PROPOSAL_MEMBER]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            list(rule.notified_proposal_roles.values_list("name", flat=True)),
            [RoleEnum.PROPOSAL_MEMBER],
        )

    def test_selection_is_embedded_in_the_workflow_step(self):
        rule = self._rule("step_rejected", "applicant", step=self.decision)
        rule.notified_proposal_roles.set([ProposalRole.MANAGER])
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            f"/api/proposal-protected-calls/{self.call.uuid.hex}/workflow_steps/"
        )
        step = next(s for s in response.data if s["step"] == "allocation_decision")
        (embedded,) = step["notification_rules"]
        self.assertEqual(
            embedded["notified_proposal_roles"], [RoleEnum.PROPOSAL_MANAGER]
        )


class ApplicantRuleDeliveryTest(ApplicantRolesMixin, NotificationRuleDeliveryTestBase):
    def setUp(self):
        super().setUp()
        self.rule = self._rule("step_rejected", "applicant", step=self.decision)
        self.instance.status = WorkflowStepInstanceStatuses.COMPLETED
        self.instance.save()
        self.decision_instance = self.proposal.workflow_step_instances.get(
            step="allocation_decision"
        )
        self.decision_instance.status = WorkflowStepInstanceStatuses.ACTIVE
        self.decision_instance.save()

    def _reject(self):
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            workflow_service.reject_at_step(
                self.proposal, self.decision_instance, "No", self.manager
            )

    def test_default_reaches_creator_and_whole_team_in_one_message(self):
        self._reject()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(
            sorted(message.to),
            sorted(
                [
                    self.applicant.email,
                    self.pi.email,
                    self.editor.email,
                    self.member.email,
                ]
            ),
        )
        self.assertEqual(message.cc, [])

    def test_selected_roles_only(self):
        self.rule.notified_proposal_roles.set([ProposalRole.MANAGER])
        self._reject()
        self.assertEqual(len(mail.outbox), 1)
        # The creator is reached through a selected role, not by default.
        self.assertEqual(mail.outbox[0].to, [self.pi.email])

    def test_creator_holding_a_selected_role_is_reached(self):
        self.proposal.add_user(self.applicant, ProposalRole.MANAGER)
        self.rule.notified_proposal_roles.set([ProposalRole.MANAGER])
        self._reject()
        self.assertEqual(
            sorted(mail.outbox[0].to), sorted([self.applicant.email, self.pi.email])
        )

    def test_revoked_role_is_not_reached(self):
        self.proposal.remove_user(self.editor, ProposalRole.ADMIN)
        self.rule.notified_proposal_roles.set([ProposalRole.ADMIN])
        self._reject()
        self.assertEqual(mail.outbox, [])

    def test_unsubscribed_holder_is_left_out(self):
        self.member.notifications_enabled = False
        self.member.save()
        self.rule.notified_proposal_roles.set([ProposalRole.MEMBER, ProposalRole.ADMIN])
        self._reject()
        self.assertEqual(mail.outbox[0].to, [self.editor.email])

    def test_other_audiences_keep_private_copies(self):
        second_manager = structure_factories.UserFactory()
        self.call.add_user(second_manager, CallRole.MANAGER)
        self._rule("step_rejected", "call_managers", step=self.decision)
        self._reject()
        applicant_messages = [m for m in mail.outbox if len(m.to) > 1]
        self.assertEqual(len(applicant_messages), 1)
        private = [m.to for m in mail.outbox if m not in applicant_messages]
        self.assertEqual(
            sorted(private),
            sorted([[self.manager.email], [second_manager.email]]),
        )


class ResponsibleApplicantRolesTest(
    ApplicantRolesMixin, NotificationRuleDeliveryTestBase
):
    def test_responsible_applicant_rule_uses_the_selection(self):
        self.decision.responsible_role = ResponsibleRoles.APPLICANT
        self.decision.save()
        rule = self._rule("step_started", "responsible_role", step=self.decision)
        rule.notified_proposal_roles.set([ProposalRole.ADMIN])
        users = notification_rules.resolve_recipients(rule, self.proposal)
        self.assertEqual(list(users), [self.editor])


class NotifiedRolesTransferTest(ApplicantRolesMixin, NotificationRuleDeliveryTestBase):
    def test_selection_travels_with_export_and_import(self):
        rule = self._rule("step_rejected", "applicant", step=self.decision)
        rule.notified_proposal_roles.set([ProposalRole.MANAGER, ProposalRole.ADMIN])

        document, _ = call_transfer.export_call(self.call)
        step = next(
            s for s in document["workflow_steps"] if s["step"] == "allocation_decision"
        )
        (exported,) = step["notification_rules"]
        self.assertEqual(
            exported["notified_proposal_roles"],
            [{"name": RoleEnum.PROPOSAL_ADMIN}, {"name": RoleEnum.PROPOSAL_MANAGER}],
        )

        imported, _, _ = call_transfer.import_call(
            document, self.fixture.manager, self.fixture.staff, name="Imported"
        )
        imported_rule = imported.workflow_steps.get(
            step="allocation_decision"
        ).notification_rules.get()
        self.assertEqual(
            sorted(
                imported_rule.notified_proposal_roles.values_list("name", flat=True)
            ),
            [RoleEnum.PROPOSAL_ADMIN, RoleEnum.PROPOSAL_MANAGER],
        )
