"""Assigning an evaluator role and moving a proposal to another panel are
checked again once the proposal is locked.

The permission check runs before the lock. A peer who may assign holders, or
move the proposal, only through their own role on it -- the panel chair
completing the rapporteurs' assignment here -- may lose it meanwhile; what
they asked for is then refused.
"""

from unittest import mock

from rest_framework import status, test

from waldur_mastermind.proposal import models, proposal_roles, serializers
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_call_panels import PanelsTestMixin


class PeerStandingLapsesTest(PanelsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_chair_step()
        proposal_roles.confirm_no_conflict(self.chair_assignment())
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "rapporteur-assignment")

    def lapse_during(self, serializer_class):
        """Revoke the chair's role once the request has passed its
        permission check, as a concurrent request committing would."""
        is_valid = serializer_class.is_valid
        chair_assignment = self.chair_assignment()

        def racing(serializer, *args, **kwargs):
            proposal_roles.revoke(chair_assignment, revoked_by=self.manager)
            return is_valid(serializer, *args, **kwargs)

        return mock.patch.object(serializer_class, "is_valid", racing)

    def test_a_peer_whose_role_lapsed_does_not_assign(self):
        lock = proposal_roles.lock_role_holders
        chair_assignment = self.chair_assignment()

        def racing(*args, **kwargs):
            # The first lock taken for the assignment: the chair's role is
            # revoked by a request that committed just before.
            if not racing.done:
                racing.done = True
                proposal_roles.revoke(chair_assignment, revoked_by=self.manager)
            return lock(*args, **kwargs)

        racing.done = False
        with mock.patch.object(proposal_roles, "lock_role_holders", racing):
            response = self.assign(self.p_1, self.rapporteur, by=self.p_chair)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.ProposalRoleAssignment.objects.filter(
                user=self.p_1, role=self.rapporteur
            ).exists()
        )

    def test_a_peer_whose_role_lapsed_does_not_move_the_proposal(self):
        with self.lapse_during(serializers.ProposalSetPanelSerializer):
            self.client.force_authenticate(self.p_chair)
            response = self.client.post(
                factories.ProposalFactory.get_url(self.proposal, action="set_panel"),
                {"panel": None},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.panel, self.physics_panel)
