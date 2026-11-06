"""Evaluator roles on a proposal and the assignment step that fills them."""

from unittest import mock

from django.core import mail
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework import serializers, status, test

from waldur_core.core.models import Notification
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.permissions.utils import get_users, has_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_transfer,
    models,
    notification_rules,
    proposal_roles,
    utils,
)
from waldur_mastermind.proposal.enums import (
    STEP_KINDS_MAP,
    COIStatuses,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalRoleEligibility,
    ProposalStates,
    ResponsibleRoles,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_call_steps import CallStepsTestMixin

ASSIGNMENTS_URL = "/api/proposal-role-assignments/"


def call_url(call, action):
    return f"http://testserver/api/proposal-protected-calls/{call.uuid.hex}/{action}/"


class EvaluatorRoleTestMixin(CallStepsTestMixin):
    """A call with a rapporteur role, picked from its panel members in an
    assignment step right after the administrative check."""

    def setUp(self):
        super().setUp()
        self.manager = self.fixture.call_manager
        self.panel_1 = self.fixture.panel_member
        self.panel_2 = self.panel_member()
        self.panel_3 = self.panel_member()
        self.role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.CALL_PANEL_MEMBER,
            min_holders=2,
            max_holders=2,
            allows_lead=True,
        )
        self.step = self.add_step(
            "rapporteur-assignment",
            StepKinds.ASSIGNMENT,
            "Rapporteur assignment",
            1,
        )
        self.step.assigns_roles.set([self.role])

    def panel_member(self):
        user = structure_factories.UserFactory()
        self.call.add_user(user, CallRole.PANEL_MEMBER)
        return user

    def reach_assignment_step(self):
        self.submit()
        response = self.complete("eligible")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.workflow_step, "rapporteur-assignment")

    def assign(self, user, by=None, role=None, is_lead=False, proposal=None):
        self.client.force_authenticate(by or self.manager)
        return self.client.post(
            factories.ProposalFactory.get_url(
                proposal or self.proposal, action="role_assignments"
            ),
            {
                "role_uuid": (role or self.role).uuid.hex,
                "user_uuid": user.uuid.hex,
                "is_lead": is_lead,
            },
        )

    def revoke(self, assignment_uuid, by=None):
        self.client.force_authenticate(by or self.manager)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="revoke_role_assignment"
            ),
            {"assignment_uuid": assignment_uuid},
        )

    def any_user_role(self, **values):
        values.setdefault("max_holders", 3)
        return models.CallProposalRole.objects.create(
            call=self.call,
            key=values.pop("key", "external"),
            name=values.pop("name", "External rapporteur"),
            eligible_from=ProposalRoleEligibility.ANY_USER,
            **values,
        )

    def can_read(self, user, proposal=None):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(proposal or self.proposal)
        )
        return response.status_code == status.HTTP_200_OK


class CallProposalRoleConfigTest(EvaluatorRoleTestMixin, test.APITestCase):
    def test_call_manager_defines_a_role(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            call_url(self.call, "proposal_roles"),
            {
                "name": "Second rapporteur",
                "eligible_from": ProposalRoleEligibility.CALL_PANEL_MEMBER,
                "max_holders": 1,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["key"], "second-rapporteur")
        self.assertEqual(response.data["min_holders"], 1)

        response = self.client.get(call_url(self.call, "proposal_roles"))
        self.assertEqual(
            [role["key"] for role in response.data],
            ["rapporteur", "second-rapporteur"],
        )

    def test_panel_member_cannot_define_a_role(self):
        self.client.force_authenticate(self.panel_1)
        response = self.client.post(
            call_url(self.call, "proposal_roles"), {"name": "Lead"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_more_required_than_allowed_holders_is_refused(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            call_url(self.call, "proposal_roles"),
            {"name": "Lead", "min_holders": 3, "max_holders": 2},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("min_holders", response.data)

    def test_role_in_use_by_a_step_cannot_be_deleted(self):
        self.client.force_authenticate(self.manager)
        response = self.client.delete(
            call_url(self.call, f"proposal_roles/{self.role.uuid.hex}")
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            models.CallProposalRole.objects.filter(pk=self.role.pk).exists()
        )

    def test_unused_role_can_be_deleted(self):
        role = self.any_user_role()
        self.client.force_authenticate(self.manager)
        response = self.client.delete(
            call_url(self.call, f"proposal_roles/{role.uuid.hex}")
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_assignment_kind_is_configured_by_its_role(self):
        self.assertIn(
            "assigns_roles", STEP_KINDS_MAP[StepKinds.ASSIGNMENT].config_fields
        )
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            "http://testserver/api/proposal-protected-calls/workflow_step_kinds/"
        )
        kinds = {kind["id"]: kind for kind in response.data}
        self.assertIn("assigns_roles", kinds[StepKinds.ASSIGNMENT]["config_fields"])
        self.assertNotIn("assigns_roles", kinds[StepKinds.ASSESSMENT]["config_fields"])

    def test_assignment_step_is_created_with_its_role(self):
        role = self.any_user_role()
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            call_url(self.call, "workflow_steps"),
            {
                "kind": StepKinds.ASSIGNMENT,
                "name": "External assignment",
                "assigns_roles": [self.role.uuid.hex, role.uuid.hex],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            [str(uuid) for uuid in response.data["assigns_roles"]],
            [self.role.uuid.hex, role.uuid.hex],
        )
        self.assertEqual(
            response.data["assigns_roles_names"], ["Rapporteur", role.name]
        )

        # Changing the step's roles replaces the list.
        response = self.client.patch(
            call_url(self.call, f"workflow_steps/{response.data['uuid']}"),
            {"assigns_roles": [role.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["assigns_roles_names"], [role.name])

    def test_only_an_assignment_step_assigns_a_role(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            call_url(self.call, "workflow_steps"),
            {
                "kind": StepKinds.DECISION,
                "name": "Chair decision",
                "assigns_roles": [self.role.uuid.hex],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("assigns_roles", response.data)

    def test_role_of_another_call_is_refused(self):
        other = models.CallProposalRole.objects.create(
            call=self.fixture.new_call, key="lead", name="Lead"
        )
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            call_url(self.call, f"workflow_steps/{self.step.uuid.hex}"),
            {"assigns_roles": [other.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("assigns_roles", response.data)

    def test_role_completer_names_the_role(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            call_url(self.call, "workflow_steps"),
            {
                "kind": StepKinds.ASSESSMENT,
                "name": "Consolidated report",
                "completer": ResponsibleRoles.PROPOSAL_ROLE,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("completer_proposal_role", response.data)

        response = self.client.post(
            call_url(self.call, "workflow_steps"),
            {
                "kind": StepKinds.ASSESSMENT,
                "name": "Consolidated report",
                "completer": ResponsibleRoles.PROPOSAL_ROLE,
                "completer_proposal_role": self.role.uuid.hex,
                "outcome_set": "done",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            str(response.data["completer_proposal_role"]), self.role.uuid.hex
        )

        # Moving the step to another completer drops the role.
        response = self.client.patch(
            call_url(self.call, f"workflow_steps/{response.data['uuid']}"),
            {"completer": ResponsibleRoles.CALL_MANAGER},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["completer_proposal_role"])


class AssignmentStepTest(EvaluatorRoleTestMixin, test.APITestCase):
    def test_completer_assigns_holders_marks_the_lead_and_completes(self):
        self.reach_assignment_step()

        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("0 of 2 required holders", str(response.data))

        first = self.assign(self.panel_1)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(first.data["assigned_in_step_name"], "Rapporteur assignment")
        second = self.assign(self.panel_2)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)

        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("lead", str(response.data))

        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="set_role_lead"),
            {"assignment_uuid": second.data["uuid"]},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["is_lead"])

        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            set(proposal_roles.proposal_role_holders(self.proposal, self.role)),
            {self.panel_1, self.panel_2},
        )

    def test_a_new_lead_replaces_the_previous_one(self):
        self.reach_assignment_step()
        first = self.assign(self.panel_1, is_lead=True)
        self.assertTrue(first.data["is_lead"])
        second = self.assign(self.panel_2, is_lead=True)
        self.assertTrue(second.data["is_lead"])
        leads = proposal_roles.active_assignments(self.proposal, self.role).filter(
            is_lead=True
        )
        self.assertEqual([a.user for a in leads], [self.panel_2])

    def test_panel_completer_assigns_while_the_step_runs(self):
        self.step.completer = ResponsibleRoles.PANEL_MEMBER
        self.step.save()
        self.reach_assignment_step()
        response = self.assign(self.panel_2, by=self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # A reviewer of the proposal does not complete this step.
        factories.ReviewFactory(
            proposal=self.proposal, reviewer=self.fixture.reviewer_1
        )
        response = self.assign(self.panel_3, by=self.fixture.reviewer_1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_completer_cannot_assign_once_the_step_is_over(self):
        self.step.completer = ResponsibleRoles.PANEL_MEMBER
        self.step.save()
        self.reach_assignment_step()
        self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)

        response = self.assign(self.panel_3, by=self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_call_manager_replaces_a_holder_outside_the_step(self):
        self.reach_assignment_step()
        first = self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)

        response = self.revoke(first.data["uuid"])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNotNone(response.data["revoked_at"])
        response = self.assign(self.panel_3, is_lead=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["assigned_in_step_name"])

        self.client.force_authenticate(self.manager)
        url = factories.ProposalFactory.get_url(
            self.proposal, action="role_assignments"
        )
        active = self.client.get(url).data
        self.assertEqual(
            {row["user_uuid"] for row in active},
            {self.panel_2.uuid.hex, self.panel_3.uuid.hex},
        )
        history = self.client.get(url, {"include_revoked": "true"}).data
        self.assertEqual(len(history), 3)

    def test_draft_proposal_gets_no_evaluators(self):
        response = self.assign(self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_candidates_show_eligibility_and_recorded_conflicts(self):
        self.reach_assignment_step()
        profile = factories.ReviewerProfileFactory(user=self.panel_2)
        factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.proposal, call=self.call
        )
        self.assign(self.panel_1)
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(
                self.proposal, action="role_assignment_candidates"
            ),
            {"role_uuid": self.role.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = {row["uuid"]: row for row in response.data}
        self.assertEqual(
            set(rows),
            {self.panel_1.uuid.hex, self.panel_2.uuid.hex, self.panel_3.uuid.hex},
        )
        self.assertTrue(rows[self.panel_1.uuid.hex]["holds_role"])
        self.assertTrue(rows[self.panel_2.uuid.hex]["has_recorded_conflict"])
        self.assertTrue(rows[self.panel_2.uuid.hex]["has_reviewer_profile"])
        self.assertFalse(rows[self.panel_3.uuid.hex]["has_reviewer_profile"])


class AssignmentRefusalTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_assignment_step()

    def test_ineligible_user_is_refused(self):
        response = self.assign(self.fixture.reviewer_1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("user", response.data)
        response = self.assign(structure_factories.UserFactory())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_more_than_max_holders_is_refused(self):
        self.assign(self.panel_1)
        self.assign(self.panel_2)
        response = self.assign(self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already has its 2 holders", str(response.data))

    def test_recorded_conflict_of_interest_is_refused(self):
        profile = factories.ReviewerProfileFactory(user=self.panel_1)
        coi = factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.proposal, call=self.call
        )
        response = self.assign(self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("conflict of interest", str(response.data))

        # A dismissed conflict no longer stands in the way.
        coi.status = COIStatuses.DISMISSED
        coi.save()
        response = self.assign(self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_applicant_team_member_is_refused(self):
        member = self.panel_member()
        self.proposal.add_user(member, ProposalRole.MEMBER)
        response = self.assign(member)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("applicant team", str(response.data))

    def test_holder_already_there_is_refused(self):
        self.assign(self.panel_1)
        response = self.assign(self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_lead_of_a_role_without_one_is_refused(self):
        role = self.any_user_role(allows_lead=False)
        response = self.assign(
            structure_factories.UserFactory(), role=role, is_lead=True
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_lead", response.data)

    def test_applicant_cannot_assign_or_see_evaluators(self):
        self.assign(self.panel_1)
        creator = self.fixture.proposal_creator
        response = self.assign(self.panel_2, by=creator)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(creator)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="role_assignments")
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_holder_cannot_assign(self):
        self.assign(self.panel_1)
        response = self.assign(self.panel_2, by=self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PeerAssignmentTest(EvaluatorRoleTestMixin, test.APITestCase):
    """The panel completes the assignment step: its members volunteer, but
    the lead and other people's roles are the lead's and the call manager's."""

    def setUp(self):
        super().setUp()
        self.step.completer = ResponsibleRoles.PANEL_MEMBER
        self.step.save()
        self.reach_assignment_step()

    def set_lead(self, assignment_uuid, by):
        self.client.force_authenticate(by)
        return self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="set_role_lead"),
            {"assignment_uuid": assignment_uuid},
        )

    def lead(self):
        return [
            assignment.user
            for assignment in proposal_roles.active_assignments(
                self.proposal, self.role
            ).filter(is_lead=True)
        ]

    def test_member_volunteers_and_leads_a_role_without_a_lead(self):
        response = self.assign(self.panel_1, by=self.panel_1, is_lead=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.assign(self.panel_2, by=self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.lead(), [self.panel_1])

    def test_peer_cannot_take_over_the_lead(self):
        self.assign(self.panel_1, is_lead=True)
        response = self.assign(self.panel_2, by=self.panel_2, is_lead=True)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        second = self.assign(self.panel_2, by=self.panel_2)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.data)
        response = self.set_lead(second.data["uuid"], by=self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.set_lead(second.data["uuid"], by=self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.lead(), [self.panel_1])

    def test_lead_hands_the_lead_over(self):
        self.assign(self.panel_1, is_lead=True)
        second = self.assign(self.panel_2, by=self.panel_2)
        response = self.set_lead(second.data["uuid"], by=self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.lead(), [self.panel_2])

    def test_lead_hands_the_lead_over_to_a_new_holder(self):
        self.assign(self.panel_1, is_lead=True)
        response = self.assign(self.panel_2, by=self.panel_1, is_lead=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.lead(), [self.panel_2])

    def test_peer_revokes_only_their_own_role(self):
        first = self.assign(self.panel_1, is_lead=True)
        second = self.assign(self.panel_2, by=self.panel_2)
        response = self.revoke(first.data["uuid"], by=self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # Not even the lead takes someone else's role away.
        response = self.revoke(second.data["uuid"], by=self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response = self.revoke(second.data["uuid"], by=self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            set(proposal_roles.proposal_role_holders(self.proposal, self.role)),
            {self.panel_1},
        )

    def test_call_manager_moves_the_lead_and_revokes_anyone(self):
        first = self.assign(self.panel_1, by=self.panel_1, is_lead=True)
        second = self.assign(self.panel_2, by=self.panel_2)
        response = self.set_lead(second.data["uuid"], by=self.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.lead(), [self.panel_2])
        response = self.revoke(first.data["uuid"])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.assign(self.panel_3, is_lead=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.lead(), [self.panel_3])

    def test_revoking_takes_the_holders_lock_as_assigning_does(self):
        first = self.assign(self.panel_1, is_lead=True)
        assignment = models.ProposalRoleAssignment.objects.get(uuid=first.data["uuid"])
        with mock.patch.object(
            proposal_roles,
            "lock_role_holders",
            wraps=proposal_roles.lock_role_holders,
        ) as lock:
            response = self.revoke(first.data["uuid"])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        lock.assert_any_call(mock.ANY, assignment.role)

    def test_a_peer_cannot_revoke_once_the_assignment_step_moved_on(self):
        self.assign(self.panel_1, is_lead=True)
        second = self.assign(self.panel_2, by=self.panel_2)
        real_lock = proposal_roles.lock_role_holders

        def step_completed_meanwhile(proposal, role):
            # The assignment step is completed between the caller's checks
            # and the revocation.
            locked = real_lock(proposal, role)
            models.ProposalWorkflowStepInstance.objects.filter(
                pk=locked.current_step_id
            ).update(status=WorkflowStepInstanceStatuses.COMPLETED)
            return real_lock(proposal, role)

        with mock.patch.object(
            proposal_roles, "lock_role_holders", side_effect=step_completed_meanwhile
        ):
            response = self.revoke(second.data["uuid"], by=self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            self.panel_2,
            set(proposal_roles.proposal_role_holders(self.proposal, self.role)),
        )


class HolderAccessTest(EvaluatorRoleTestMixin, test.APITestCase):
    """Holders without a call role read the proposal through their role alone."""

    def setUp(self):
        super().setUp()
        self.external = self.any_user_role()
        self.reach_assignment_step()
        self.holder = structure_factories.UserFactory()
        self.bystander = structure_factories.UserFactory()

    def test_holder_reads_the_proposal_and_a_revoked_one_does_not(self):
        self.assertFalse(self.can_read(self.holder))
        response = self.assign(self.holder, role=self.external)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(self.can_read(self.holder))
        self.assertFalse(self.can_read(self.bystander))

        self.client.force_authenticate(self.holder)
        listed = self.client.get(factories.ProposalFactory.get_list_url()).data
        self.assertEqual([p["uuid"] for p in listed], [self.proposal.uuid.hex])
        as_role = self.client.get(
            factories.ProposalFactory.get_list_url(),
            {"as_role": ResponsibleRoles.PROPOSAL_ROLE},
        ).data
        self.assertEqual([p["uuid"] for p in as_role], [self.proposal.uuid.hex])

        self.revoke(response.data["uuid"])
        self.assertFalse(self.can_read(self.holder))

    def test_holder_gets_the_calls_applicant_concealment(self):
        models.CallApplicantVisibilityConfig.objects.update_or_create(
            call=self.call,
            defaults={
                "expose_full_name": False,
                "expose_username": False,
                "expose_email": False,
            },
        )
        self.assign(self.holder, role=self.external)
        self.client.force_authenticate(self.holder)
        data = self.client.get(factories.ProposalFactory.get_url(self.proposal)).data
        self.assertNotIn("created_by_name", data)
        self.assertNotIn("created_by_uuid", data)

        self.client.force_authenticate(self.manager)
        data = self.client.get(factories.ProposalFactory.get_url(self.proposal)).data
        self.assertIn("created_by_name", data)

    def test_holder_reads_the_compliance_answers_like_a_reviewer(self):
        url = factories.ProposalFactory.get_url(self.proposal, action="checklist")
        response = self.assign(self.holder, role=self.external)
        self.client.force_authenticate(self.holder)
        self.assertNotEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

        self.revoke(response.data["uuid"])
        self.client.force_authenticate(self.holder)
        self.assertIn(
            self.client.get(url).status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_holders_are_not_the_applicant_team(self):
        self.assign(self.holder, role=self.external)
        self.assertFalse(has_user(self.proposal, self.holder))
        self.assertNotIn(self.holder, get_users(self.proposal))
        self.assertNotIn(
            self.holder, notification_rules._applicant_users(self.proposal)
        )

        # Nor do they show in the team list, which the holder reads concealed.
        self.client.force_authenticate(self.fixture.staff)
        rows = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="list_users")
        ).data
        self.assertNotIn(self.holder.uuid.hex, {row["user_uuid"] for row in rows})

    def test_holders_are_not_mapped_onto_the_awarded_project(self):
        self.assign(self.holder, role=self.external)
        self.proposal.project = None
        self.proposal.save()
        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)
        self.proposal.refresh_from_db()
        self.assertIsNotNone(self.proposal.project)
        self.assertFalse(has_user(self.proposal.project, self.holder))
        self.assertTrue(has_user(self.proposal.project, self.proposal.created_by))

    def test_holder_finds_their_assignments(self):
        self.assign(self.holder, role=self.external)
        self.client.force_authenticate(self.holder)
        response = self.client.get(ASSIGNMENTS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["proposal_uuid"], self.proposal.uuid.hex)
        self.assertEqual(response.data[0]["role_name"], "External rapporteur")

        self.client.force_authenticate(self.bystander)
        self.assertEqual(self.client.get(ASSIGNMENTS_URL).data, [])


class CoiConfirmationTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.external = self.any_user_role()
        self.step.assigns_roles.set([self.external])
        self.step.requires_coi_confirmation = True
        self.step.save()
        self.reach_assignment_step()
        self.holder = structure_factories.UserFactory()

    def confirm(self, assignment_uuid, user=None):
        self.client.force_authenticate(user or self.holder)
        return self.client.post(
            f"{ASSIGNMENTS_URL}{assignment_uuid}/confirm_no_conflict/"
        )

    def test_holder_confirms_before_gaining_access(self):
        response = self.assign(self.holder, role=self.external)
        self.assertTrue(response.data["awaits_coi_confirmation"])
        self.assertFalse(self.can_read(self.holder))

        # Someone else cannot confirm for them.
        self.assertEqual(
            self.confirm(response.data["uuid"], user=self.manager).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        confirmed = self.confirm(response.data["uuid"])
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK, confirmed.data)
        self.assertIsNotNone(confirmed.data["coi_confirmed_at"])
        # No reviewer profile: nothing on record could be checked.
        self.assertTrue(confirmed.data["coi_declaration_only"])
        self.assertTrue(self.can_read(self.holder))

    def test_holder_with_a_profile_is_checked_against_records(self):
        profile = factories.ReviewerProfileFactory(user=self.holder)
        response = self.assign(self.holder, role=self.external)
        coi = factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.proposal, call=self.call
        )
        self.assertEqual(
            self.confirm(response.data["uuid"]).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        coi.status = COIStatuses.WAIVED
        coi.save()
        confirmed = self.confirm(response.data["uuid"])
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK, confirmed.data)
        self.assertFalse(confirmed.data["coi_declaration_only"])

    def test_holder_confirms_the_steps_declaration_as_worded_then(self):
        self.call.coi_declaration_text = "The call's declaration."
        self.call.save()
        response = self.assign(self.holder, role=self.external)
        self.assertEqual(
            response.data["coi_declaration_text"], "The call's declaration."
        )
        self.step.coi_declaration_text = "The step's declaration."
        self.step.save()
        self.client.force_authenticate(self.holder)
        listed = self.client.get(ASSIGNMENTS_URL).data
        self.assertEqual(listed[0]["coi_declaration_text"], "The step's declaration.")

        confirmed = self.confirm(response.data["uuid"])
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK, confirmed.data)
        self.assertEqual(
            confirmed.data["coi_declaration_text"], "The step's declaration."
        )
        self.step.coi_declaration_text = "Reworded later."
        self.step.save()
        assignment = models.ProposalRoleAssignment.objects.get(
            uuid=response.data["uuid"]
        )
        self.assertEqual(assignment.coi_declaration_text, "The step's declaration.")

    def test_without_wording_the_platforms_declaration_is_confirmed(self):
        response = self.assign(self.holder, role=self.external)
        self.assertEqual(response.data["coi_declaration_text"], "")
        confirmed = self.confirm(response.data["uuid"])
        self.assertEqual(confirmed.data["coi_declaration_text"], "")

    def test_another_holder_does_not_read_what_was_confirmed(self):
        response = self.assign(self.holder, role=self.external)
        self.confirm(response.data["uuid"])
        other = structure_factories.UserFactory()
        self.confirm(self.assign(other, role=self.external).data["uuid"], other)
        self.client.force_authenticate(other)
        rows = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="role_assignments")
        ).data
        row = next(r for r in rows if r["uuid"] == response.data["uuid"])
        self.assertIsNone(row["coi_declaration_text"])

    def test_unconfirmed_holder_counts_for_the_gate_but_completes_no_step(self):
        self.external.min_holders = 1
        self.external.save()
        self.assign(self.holder, role=self.external)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)


class HolderCompletesStepTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.report = self.add_step(
            "consolidated-report",
            StepKinds.ASSESSMENT,
            "Consolidated report",
            2,
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role,
            outcome_set="done",
        )
        self.reach_assignment_step()
        self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")

    def test_holder_acts_on_their_step_and_others_do_not(self):
        response = self.complete("done", user=self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.complete("done", user=self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_revoked_holder_no_longer_acts(self):
        assignment = proposal_roles.active_assignments(self.proposal, self.role).get(
            user=self.panel_2
        )
        self.revoke(assignment.uuid.hex)
        response = self.complete("done", user=self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_holders_are_the_steps_responsible_users(self):
        users = notification_rules._responsible_role_users(self.report, self.proposal)
        self.assertEqual(set(users), {self.panel_1, self.panel_2})


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class HolderNotificationTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        for key in (
            "proposal.proposal_role_assigned",
            "proposal.proposal_role_revoked",
            "proposal.workflow_step_event",
        ):
            Notification.objects.update_or_create(key=key, defaults={"enabled": True})
        self.reach_assignment_step()
        mail.outbox = []

    def test_holder_is_told_of_assignment_and_revocation(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.assign(self.panel_1, is_lead=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.panel_1.email])
        self.assertIn("Rapporteur", mail.outbox[0].subject)
        self.assertIn("(lead)", mail.outbox[0].body)

        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.revoke(response.data["uuid"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.panel_1.email])
        self.assertIn("no longer", mail.outbox[0].subject)

    def test_rule_addresses_role_holders_and_never_applicant_mail(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assign(self.panel_1, is_lead=True)
            self.assign(self.panel_2)
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        allocation = self.call.workflow_steps.get(key="allocation_decision")
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.step,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.PROPOSAL_ROLE,
            proposal_role=self.role,
        )
        applicant_rule = models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=allocation,
            trigger=NotificationRuleTriggers.STEP_STARTED,
            recipient=NotificationRuleRecipients.APPLICANT,
        )
        self.assertNotIn(
            self.panel_1,
            notification_rules.resolve_recipients(applicant_rule, self.proposal),
        )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        recipients = [message.to for message in mail.outbox]
        self.assertIn([self.panel_1.email], recipients)
        self.assertIn([self.panel_2.email], recipients)
        self.assertNotIn([self.panel_3.email], recipients)

    def test_rule_api_needs_the_role_for_the_role_audience(self):
        self.client.force_authenticate(self.manager)
        url = "http://testserver/api/call-workflow-step-notification-rules/"
        payload = {
            "workflow_step": self.step.uuid.hex,
            "trigger": NotificationRuleTriggers.STEP_STARTED,
            "recipient": NotificationRuleRecipients.PROPOSAL_ROLE,
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("proposal_role", response.data)

        payload["proposal_role"] = self.role.uuid.hex
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["proposal_role_name"], "Rapporteur")
        # A second role is an audience of its own; the same one is a duplicate.
        other = self.any_user_role()
        response = self.client.post(url, {**payload, "proposal_role": other.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ResolverTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_assignment_step()
        self.assign(self.panel_1)

    def test_role_refs(self):
        for ref in (
            self.role,
            self.role.pk,
            self.role.uuid.hex,
            self.role.key,
            f"proposal_role:{self.role.pk}",
            f"proposal_role:{self.role.uuid.hex}",
        ):
            self.assertEqual(
                list(proposal_roles.proposal_role_holders(self.proposal, ref)),
                [self.panel_1],
                ref,
            )
        self.assertEqual(
            list(proposal_roles.proposal_role_holders(self.proposal, "unknown")), []
        )
        other = models.CallProposalRole.objects.create(
            call=self.fixture.new_call, key="rapporteur", name="Rapporteur"
        )
        self.assertEqual(
            list(proposal_roles.proposal_role_holders(self.proposal, other)), []
        )

    def test_a_call_without_roles_has_no_holders(self):
        self.assertEqual(
            list(proposal_roles.proposal_role_holders(self.fixture.proposal_submitted)),
            [],
        )


class CopyEvaluatorRolesTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.report = self.add_step(
            "consolidated-report",
            StepKinds.ASSESSMENT,
            "Consolidated report",
            2,
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role,
            outcome_set="done",
        )
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.report,
            trigger=NotificationRuleTriggers.STEP_STARTED,
            recipient=NotificationRuleRecipients.PROPOSAL_ROLE,
            proposal_role=self.role,
        )

    def assert_copied(self, call):
        role = call.proposal_roles.get(key="rapporteur")
        self.assertNotEqual(role.pk, self.role.pk)
        self.assertEqual(
            (role.name, role.eligible_from, role.min_holders, role.max_holders),
            ("Rapporteur", ProposalRoleEligibility.CALL_PANEL_MEMBER, 2, 2),
        )
        self.assertTrue(role.allows_lead)
        self.assertEqual(
            list(
                call.workflow_steps.get(key="rapporteur-assignment").assigns_roles.all()
            ),
            [role],
        )
        self.assertEqual(
            call.workflow_steps.get(key="consolidated-report").completer_proposal_role,
            role,
        )
        return role

    def test_duplicate_call_copies_roles(self):
        copy = utils.duplicate_call(self.call, "Copy", self.fixture.staff)
        self.assert_copied(copy)

    def test_export_and_import_carry_roles(self):
        document, _ = call_transfer.export_call(self.call)
        self.assertEqual(
            [role["key"] for role in document["proposal_roles"]], ["rapporteur"]
        )
        call, _, warnings = call_transfer.import_call(
            document, self.call.manager, self.fixture.staff
        )
        self.assertEqual(warnings, [])
        role = self.assert_copied(call)
        rule = models.CallWorkflowStepNotificationRule.objects.get(
            workflow_step__call=call,
            recipient=NotificationRuleRecipients.PROPOSAL_ROLE,
        )
        self.assertEqual(rule.proposal_role, role)

    def test_import_refuses_an_unknown_role(self):
        document, _ = call_transfer.export_call(self.call)
        document["proposal_roles"] = []
        with self.assertRaises(serializers.ValidationError):
            call_transfer.import_call(document, self.call.manager, self.fixture.staff)
        self.assertEqual(models.Call.objects.filter(name=self.call.name).count(), 1)


class CallWithoutRolesTest(CallStepsTestMixin, test.APITestCase):
    def test_workflow_and_access_are_unchanged(self):
        self.submit()
        self.assertEqual(self.complete("eligible").status_code, status.HTTP_200_OK)
        self.assertEqual(list(proposal_roles.proposals_held_by(self.fixture.staff)), [])
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)


class OneRolePerPersonTest(EvaluatorRoleTestMixin, test.APITestCase):
    """One person holds at most one evaluator role on a proposal."""

    def setUp(self):
        super().setUp()
        self.second = models.CallProposalRole.objects.create(
            call=self.call,
            key="second",
            name="Second rapporteur",
            eligible_from=ProposalRoleEligibility.CALL_PANEL_MEMBER,
            max_holders=1,
        )
        self.step.assigns_roles.add(self.second)
        self.reach_assignment_step()

    def test_holder_of_one_role_cannot_take_another(self):
        self.assertEqual(self.assign(self.panel_1).status_code, 201)
        response = self.assign(self.panel_1, role=self.second)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already holds the role 'Rapporteur'", str(response.data))
        self.assertIn("one evaluator role per proposal", str(response.data))

    def test_revoked_holder_can_take_another_role(self):
        first = self.assign(self.panel_1)
        self.revoke(first.data["uuid"])
        response = self.assign(self.panel_1, role=self.second)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_candidates_show_holders_of_another_role_as_unavailable(self):
        self.assign(self.panel_1)
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(
                self.proposal, action="role_assignment_candidates"
            ),
            {"role_uuid": self.second.uuid.hex},
        )
        rows = {row["uuid"]: row for row in response.data}
        holder = rows[self.panel_1.uuid.hex]
        self.assertFalse(holder["holds_role"])
        self.assertTrue(holder["holds_other_role"])
        self.assertEqual(holder["held_role_name"], "Rapporteur")
        self.assertFalse(rows[self.panel_2.uuid.hex]["holds_other_role"])

    def test_the_database_refuses_a_second_active_role(self):
        proposal_roles.assign(
            self.proposal, self.role, self.panel_1, assigned_by=self.manager
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.ProposalRoleAssignment.objects.create(
                proposal=self.proposal, role=self.second, user=self.panel_1
            )


class StepAssigningSeveralRolesTest(EvaluatorRoleTestMixin, test.APITestCase):
    """One rapporteur assignment step fills the lead and the second
    rapporteur together."""

    def setUp(self):
        super().setUp()
        self.role.min_holders = 1
        self.role.max_holders = 1
        self.role.save()
        self.second = models.CallProposalRole.objects.create(
            call=self.call,
            key="second",
            name="Second rapporteur",
            eligible_from=ProposalRoleEligibility.CALL_PANEL_MEMBER,
            max_holders=1,
        )
        self.step.assigns_roles.add(self.second)
        self.step.completer = ResponsibleRoles.PANEL_MEMBER
        self.step.save()
        self.reach_assignment_step()

    def test_gate_needs_every_role_filled(self):
        response = self.assign(self.panel_2, by=self.panel_1, is_lead=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["assigned_in_step_name"], "Rapporteur assignment"
        )

        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Second rapporteur", str(response.data))

        response = self.assign(self.panel_3, by=self.panel_1, role=self.second)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["assigned_in_step_name"], "Rapporteur assignment"
        )

        # A panel member completes the step they assigned in.
        response = self.complete("done", user=self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_role_the_step_does_not_assign_is_for_managers_only(self):
        other = self.any_user_role()
        response = self.assign(
            structure_factories.UserFactory(), by=self.panel_1, role=other
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_new_holders_confirm_when_the_step_asks_for_it(self):
        self.step.requires_coi_confirmation = True
        self.step.save()
        self.assertTrue(proposal_roles.coi_confirmation_required(self.second))
        response = self.assign(self.panel_3, role=self.second)
        self.assertTrue(response.data["awaits_coi_confirmation"])


class ConflictAfterAssignmentTest(EvaluatorRoleTestMixin, test.APITestCase):
    """A conflict of interest recorded after the assignment stops the access
    it gave; recusing revokes it."""

    def setUp(self):
        super().setUp()
        # A holder with no call role, who reads the proposal through the
        # evaluator role alone.
        self.external = self.any_user_role(min_holders=1)
        self.step.assigns_roles.set([self.external])
        self.report = self.add_step(
            "consolidated-report",
            StepKinds.ASSESSMENT,
            "Consolidated report",
            2,
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.external,
            outcome_set="done",
        )
        self.reach_assignment_step()
        self.holder = structure_factories.UserFactory()
        response = self.assign(self.holder, role=self.external, is_lead=False)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")
        self.profile = factories.ReviewerProfileFactory(user=self.holder)

    def record_conflict(self):
        return factories.ConflictOfInterestFactory(
            reviewer=self.profile, proposal=self.proposal, call=self.call
        )

    def test_pending_conflict_stops_reading_and_acting(self):
        self.assertTrue(self.can_read(self.holder))
        coi = self.record_conflict()
        self.assertFalse(proposal_roles.holds_role(self.holder, self.proposal))
        # Those overseeing the call see why.
        self.client.force_authenticate(self.manager)
        rows = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="role_assignments")
        ).data
        self.assertTrue(rows[0]["has_standing_conflict"])
        self.assertFalse(self.can_read(self.holder))
        response = self.complete("done", user=self.holder)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertNotIn(
            self.holder,
            notification_rules._responsible_role_users(self.report, self.proposal),
        )
        self.assertNotIn(
            self.proposal.pk, set(proposal_roles.proposals_held_by(self.holder))
        )

        # Dismissed, the role gives its access again.
        coi.status = COIStatuses.DISMISSED
        coi.save()
        self.assertTrue(self.can_read(self.holder))
        response = self.complete("done", user=self.holder)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_recusal_revokes_the_role_and_says_why(self):
        coi = self.record_conflict()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            f"/api/conflicts-of-interest/{coi.uuid.hex}/recuse/",
            {"status": "recused", "review_notes": "Former colleague."},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        assignment = models.ProposalRoleAssignment.objects.get(
            proposal=self.proposal, user=self.holder
        )
        self.assertIsNotNone(assignment.revoked_at)
        self.assertEqual(assignment.revoked_by, self.fixture.staff)
        self.assertEqual(assignment.revoked_reason, proposal_roles.RECUSAL_REASON)
        self.assertFalse(self.can_read(self.holder))


class EvaluatorPrivacyTest(EvaluatorRoleTestMixin, test.APITestCase):
    """Who evaluates a proposal is not for its applicant team, and another
    holder's email and conflict state are not for a fellow holder."""

    def setUp(self):
        super().setUp()
        self.step.completer = ResponsibleRoles.PANEL_MEMBER
        self.step.save()
        self.reach_assignment_step()
        self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)

    def listed_for(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(ASSIGNMENTS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["proposal_uuid"] for row in response.data}

    def test_call_manager_who_wrote_the_proposal_does_not_see_its_evaluators(self):
        self.assertIn(self.proposal.uuid.hex, self.listed_for(self.manager))

        self.proposal.created_by = self.manager
        self.proposal.save()
        self.proposal.add_user(self.manager, ProposalRole.MANAGER)
        self.assertNotIn(self.proposal.uuid.hex, self.listed_for(self.manager))

    def test_call_manager_on_the_proposal_team_does_not_see_its_evaluators(self):
        self.proposal.add_user(self.manager, ProposalRole.MEMBER)
        self.assertNotIn(self.proposal.uuid.hex, self.listed_for(self.manager))
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="role_assignments")
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_holder_sees_fellow_holders_without_their_email_or_conflict_state(self):
        self.client.force_authenticate(self.panel_1)
        rows = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="role_assignments")
        ).data
        rows = {row["user_uuid"]: row for row in rows}
        own, other = rows[self.panel_1.uuid.hex], rows[self.panel_2.uuid.hex]
        self.assertEqual(own["user_email"], self.panel_1.email)
        self.assertIsNotNone(own["coi_confirmation_required"])
        self.assertIsNone(other["user_email"])
        self.assertIsNone(other["coi_confirmation_required"])
        self.assertIsNone(other["awaits_coi_confirmation"])
        self.assertEqual(other["user_full_name"], self.panel_2.full_name)

        self.client.force_authenticate(self.manager)
        rows = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="role_assignments")
        ).data
        self.assertEqual(
            {row["user_email"] for row in rows},
            {self.panel_1.email, self.panel_2.email},
        )

    def test_why_a_fellow_holder_was_revoked_is_not_for_a_holder(self):
        assignment = proposal_roles.active_assignments(self.proposal).get(
            user=self.panel_2
        )
        proposal_roles.revoke(
            assignment, self.manager, reason=proposal_roles.RECUSAL_REASON
        )
        url = factories.ProposalFactory.get_url(
            self.proposal, action="role_assignments"
        )

        def revoked_reason(user):
            self.client.force_authenticate(user)
            rows = self.client.get(url, {"include_revoked": "true"}).data
            [row] = [row for row in rows if row["uuid"] == assignment.uuid.hex]
            return row["revoked_reason"]

        self.assertEqual(revoked_reason(self.panel_1), "")
        self.assertEqual(revoked_reason(self.manager), proposal_roles.RECUSAL_REASON)


class CandidateSearchTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.step.completer = ResponsibleRoles.PANEL_MEMBER
        self.step.save()
        self.reach_assignment_step()
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="role_assignment_candidates"
        )

    def candidates(self, role, by=None, **query):
        self.client.force_authenticate(by or self.manager)
        response = self.client.get(self.url, {"role_uuid": role.uuid.hex, **query})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_call_role_limited_role_lists_without_a_search(self):
        rows = self.candidates(self.role)
        self.assertEqual(
            {row["uuid"] for row in rows},
            {self.panel_1.uuid.hex, self.panel_2.uuid.hex, self.panel_3.uuid.hex},
        )

    def test_search_matches_name_email_and_username(self):
        self.panel_1.username = "unique-rapporteur-one"
        self.panel_1.save()
        self.panel_2.first_name = "Zeldarina"
        self.panel_2.save()
        self.panel_3.email = "rapporteur.three@example.org"
        self.panel_3.save()
        self.assertEqual(
            [row["uuid"] for row in self.candidates(self.role, query="zeLDarina")],
            [self.panel_2.uuid.hex],
        )
        self.assertEqual(
            [row["uuid"] for row in self.candidates(self.role, query="RAPPORTEUR.th")],
            [self.panel_3.uuid.hex],
        )
        self.assertEqual(
            [
                row["uuid"]
                for row in self.candidates(self.role, query="UNIQUE-rapporteur")
            ],
            [self.panel_1.uuid.hex],
        )

    def test_any_user_role_lists_nobody_until_searched(self):
        role = self.any_user_role()
        outsider = structure_factories.UserFactory(first_name="Quentinella")
        self.assertEqual(self.candidates(role), [])
        rows = self.candidates(role, query="quentinELLA")
        self.assertEqual([row["uuid"] for row in rows], [outsider.uuid.hex])

    def test_applicant_team_is_left_out(self):
        self.proposal.add_user(self.panel_3, ProposalRole.MEMBER)
        rows = self.candidates(self.role)
        self.assertNotIn(self.panel_3.uuid.hex, {row["uuid"] for row in rows})

    def test_email_is_for_those_overseeing_the_call(self):
        rows = self.candidates(self.role)
        self.assertTrue(all(row["email"] for row in rows))
        rows = self.candidates(self.role, by=self.panel_1)
        self.assertTrue(rows)
        self.assertTrue(all(row["email"] is None for row in rows))

    def test_a_completer_finds_by_email_only_the_whole_address(self):
        role = self.any_user_role()
        self.step.assigns_roles.add(role)
        outsider = structure_factories.UserFactory(email="hidden.person@example.org")
        rows = self.candidates(role, by=self.panel_1, query="hidden.pers")
        self.assertEqual(rows, [])
        rows = self.candidates(role, by=self.panel_1, query="HIDDEN.person@example.org")
        self.assertEqual([row["uuid"] for row in rows], [outsider.uuid.hex])
        self.assertIsNone(rows[0]["email"])
        # Staff, who see every address, search them in part.
        staff = structure_factories.UserFactory(is_staff=True)
        rows = self.candidates(role, by=staff, query="hidden.pers")
        self.assertEqual([row["uuid"] for row in rows], [outsider.uuid.hex])

    def test_recorded_conflicts_are_for_those_overseeing_the_call(self):
        profile = factories.ReviewerProfileFactory(user=self.panel_3)
        factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.proposal, call=self.call
        )
        rows = {row["uuid"]: row for row in self.candidates(self.role)}
        self.assertTrue(rows[self.panel_3.uuid.hex]["has_recorded_conflict"])
        self.assertFalse(rows[self.panel_2.uuid.hex]["has_recorded_conflict"])
        rows = self.candidates(self.role, by=self.panel_1)
        self.assertTrue(rows)
        self.assertTrue(all(row["has_recorded_conflict"] is None for row in rows))

    def test_query_count_does_not_grow_with_the_candidates(self):
        self.client.force_authenticate(self.manager)
        params = {"role_uuid": self.role.uuid.hex}
        self.client.get(self.url, params)
        with CaptureQueriesContext(connection) as few:
            self.client.get(self.url, params)
        for _ in range(5):
            self.panel_member()
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.url, params)
        self.assertEqual(len(response.data), 8)
        self.assertEqual(len(many), len(few))


class AssignmentStateAndLeadTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_assignment_step()

    def test_settled_proposal_gets_no_new_evaluators(self):
        for state in (
            ProposalStates.CANCELED,
            ProposalStates.REJECTED,
            ProposalStates.ACCEPTED,
        ):
            models.Proposal.objects.filter(pk=self.proposal.pk).update(state=state)
            response = self.assign(self.panel_1)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, state)
            self.assertIn("no longer assigned", str(response.data))

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def test_new_lead_is_told(self):
        Notification.objects.update_or_create(
            key="proposal.proposal_role_assigned", defaults={"enabled": True}
        )
        self.assign(self.panel_1, is_lead=True)
        second = self.assign(self.panel_2)
        mail.outbox = []
        self.client.force_authenticate(self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.ProposalFactory.get_url(
                    self.proposal, action="set_role_lead"
                ),
                {"assignment_uuid": second.data["uuid"]},
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [message.to for message in mail.outbox], [[self.panel_2.email]]
        )
        self.assertIn("(lead)", mail.outbox[0].body)


class NotificationRuleUniquenessTest(EvaluatorRoleTestMixin, test.APITestCase):
    def test_one_rule_per_audience_in_the_database(self):
        values = {
            "workflow_step": self.step,
            "trigger": NotificationRuleTriggers.STEP_STARTED,
        }
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step=self.step
        ).delete()
        models.CallWorkflowStepNotificationRule.objects.create(
            recipient=NotificationRuleRecipients.CALL_MANAGERS, **values
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.CallWorkflowStepNotificationRule.objects.create(
                recipient=NotificationRuleRecipients.CALL_MANAGERS, **values
            )
        other = self.any_user_role()
        for role in (self.role, other):
            models.CallWorkflowStepNotificationRule.objects.create(
                recipient=NotificationRuleRecipients.PROPOSAL_ROLE,
                proposal_role=role,
                **values,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.CallWorkflowStepNotificationRule.objects.create(
                recipient=NotificationRuleRecipients.PROPOSAL_ROLE,
                proposal_role=self.role,
                **values,
            )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class EvaluatorStepMailLinkTest(EvaluatorRoleTestMixin, test.APITestCase):
    def test_holders_are_linked_to_the_proposal_itself(self):
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.reach_assignment_step()
        self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)
        models.CallWorkflowStepNotificationRule.objects.filter(
            workflow_step__call=self.call
        ).delete()
        for recipient, role in (
            (NotificationRuleRecipients.PROPOSAL_ROLE, self.role),
            (NotificationRuleRecipients.CALL_MANAGERS, None),
        ):
            models.CallWorkflowStepNotificationRule.objects.create(
                workflow_step=self.step,
                trigger=NotificationRuleTriggers.STEP_COMPLETED,
                recipient=recipient,
                proposal_role=role,
            )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        bodies = {tuple(message.to): message.body for message in mail.outbox}
        holder_mail = bodies[(self.panel_1.email,)]
        self.assertIn(f"proposals/{self.proposal.uuid.hex}/", holder_mail)
        self.assertNotIn("call-management", holder_mail)
        self.assertIn("call-management", bodies[(self.manager.email,)])
