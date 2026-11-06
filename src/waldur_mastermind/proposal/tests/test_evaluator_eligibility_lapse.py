"""An evaluator role ends with the eligibility it was held through.

A role held through a call role or a call panel is revoked, with its reason,
when its holder loses that call role or is taken off the panel; a role open
to any user is not.
"""

from rest_framework import status, test

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import UserRole
from waldur_mastermind.proposal import models, proposal_roles
from waldur_mastermind.proposal.tests.test_call_panels import PanelsTestMixin
from waldur_mastermind.proposal.tests.test_proposal_roles import (
    EvaluatorRoleTestMixin,
)


class CallRoleLapseTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_assignment_step()

    def remove_from_call(self, user):
        user_role = UserRole.objects.get(
            user=user,
            object_id=self.call.pk,
            role__name=RoleEnum.CALL_PANEL_MEMBER,
            is_active=True,
        )
        self.call.remove_user(user, user_role.role)

    def test_a_panel_member_taken_off_the_call_loses_the_role(self):
        response = self.assign(self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(proposal_roles.holds_role(self.panel_2, self.proposal))

        self.remove_from_call(self.panel_2)

        self.assertFalse(proposal_roles.holds_role(self.panel_2, self.proposal))
        assignment = models.ProposalRoleAssignment.objects.get(
            user=self.panel_2, proposal=self.proposal
        )
        self.assertIsNotNone(assignment.revoked_at)
        self.assertEqual(
            assignment.revoked_reason, proposal_roles.LOST_ELIGIBILITY_REASON
        )
        self.assertFalse(self.can_read(self.panel_2))

    def test_a_role_open_to_any_user_is_kept(self):
        external = self.any_user_role()
        self.step.assigns_roles.add(external)
        response = self.assign(self.panel_2, role=external)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.remove_from_call(self.panel_2)

        self.assertTrue(
            proposal_roles.holds_role(self.panel_2, self.proposal, external)
        )


class PanelLapseTest(PanelsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_chair_step()
        proposal_roles.confirm_no_conflict(self.chair_assignment())
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        response = self.assign(self.p_1, self.rapporteur, by=self.p_chair)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def update_panel(self, **payload):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(
            self.panels_url(self.physics_panel),
            {"name": self.physics_panel.name, **payload},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_member_taken_off_the_panel_loses_the_role(self):
        self.update_panel(
            members=[self.p_chair.uuid.hex, self.p_2.uuid.hex],
        )

        self.assertFalse(
            proposal_roles.holds_role(self.p_1, self.proposal, self.rapporteur)
        )
        assignment = models.ProposalRoleAssignment.objects.get(
            user=self.p_1, role=self.rapporteur
        )
        self.assertEqual(
            assignment.revoked_reason, proposal_roles.LOST_ELIGIBILITY_REASON
        )

    def test_a_replaced_chair_loses_the_chair_role(self):
        self.update_panel(chair=self.p_2.uuid.hex)

        self.assertIsNone(self.chair_assignment())
        # Still a member, they keep the role held as one.
        self.assertTrue(
            proposal_roles.holds_role(self.p_1, self.proposal, self.rapporteur)
        )
