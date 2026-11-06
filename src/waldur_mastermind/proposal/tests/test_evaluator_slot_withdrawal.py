"""An evaluator who loses their role, or turns out to be conflicted, loses
the slots it gave them.

Two holders of an evaluator role each assess the proposal in a step whose
outcome their contributions decide. One of them is revoked, recused, or
has a conflict of interest recorded against them.
"""

from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions import tasks as permission_tasks
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    contributions,
    managers,
    models,
    my_tasks,
    notification_rules,
    proposal_roles,
    workflow_service,
)
from waldur_mastermind.proposal import permissions as proposal_permissions
from waldur_mastermind.proposal.enums import (
    COIStatuses,
    ContributorSources,
    OutcomeSets,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
    WorkflowStepOutcomes,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_proposal_roles import (
    EvaluatorRoleTestMixin,
)

Statuses = StepContributionStatuses


class EvaluatorSlotWithdrawalTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        # Holders with no call role, who read the proposal through the
        # evaluator role alone.
        self.external = self.any_user_role(min_holders=2, max_holders=2)
        self.step.assigns_roles.set([self.external])
        self.assessment = self.add_step(
            "assessment",
            StepKinds.ASSESSMENT,
            "Assessment",
            2,
            outcome_set=OutcomeSets.PER_OFFERING,
            contributor_source=f"{ContributorSources.PROPOSAL_ROLE}:{self.external.pk}",
            completer=ResponsibleRoles.CALL_MANAGER,
        )
        self.reach_assignment_step()
        self.first = structure_factories.UserFactory()
        self.second = structure_factories.UserFactory()
        for user in (self.first, self.second):
            response = self.assign(user, role=self.external)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, response.data
            )
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "assessment")
        self.instance = self.proposal.workflow_step_instances.get(
            call_step=self.assessment
        )
        self.assertEqual(
            self.slots(), {self.first: Statuses.PENDING, self.second: Statuses.PENDING}
        )

    def slots(self):
        return {
            contribution.contributor: contribution.status
            for contribution in self.instance.contributions.all()
        }

    def slot_of(self, user):
        return self.instance.contributions.get(contributor=user)

    def submit_contribution(self, user, outcome):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="submit_contribution"
            ),
            {"step_uuid": self.assessment.uuid.hex, "outcome": outcome},
            format="json",
        )

    def revoke_first(self):
        assignment = proposal_roles.active_assignments(
            self.proposal, self.external
        ).get(user=self.first)
        response = self.revoke(assignment.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def record_conflict(self, user=None):
        profile = factories.ReviewerProfileFactory(user=user or self.first)
        return factories.ConflictOfInterestFactory(
            reviewer=profile,
            proposal=self.proposal,
            call=self.call,
            status=COIStatuses.PENDING,
        )

    def step_state(self):
        self.instance.refresh_from_db()
        return self.instance.status, self.instance.outcome

    def test_revoking_withdraws_the_open_slot_and_refuses_the_holder(self):
        self.revoke_first()
        self.assertEqual(
            self.slots(),
            {self.first: Statuses.EXPIRED, self.second: Statuses.PENDING},
        )
        response = self.submit_contribution(self.first, "rejected")
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertEqual(self.slot_of(self.first).status, Statuses.EXPIRED)
        self.assertFalse(self.can_read(self.first))

    def decline(self, user, contribution):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="decline_contribution"
            ),
            {"contribution_uuid": contribution.uuid.hex},
            format="json",
        )

    def test_a_slot_the_call_manager_withdrew_stays_withdrawn(self):
        response = self.decline(self.fixture.call_manager, self.slot_of(self.first))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.slot_of(self.first).status, Statuses.WITHDRAWN)
        # A conflict recorded and dismissed again, or their role confirmed,
        # gives back only what the system took.
        coi = self.record_conflict()
        coi.status = COIStatuses.DISMISSED
        coi.save()
        contributions.sync_role_slots(self.first, self.proposal)
        self.assertEqual(self.slot_of(self.first).status, Statuses.WITHDRAWN)
        response = self.submit_contribution(self.first, "accepted")
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
        )

    def test_a_slot_its_contributor_declined_stays_declined(self):
        response = self.decline(self.first, self.slot_of(self.first))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        coi = self.record_conflict()
        coi.status = COIStatuses.DISMISSED
        coi.save()
        self.assertEqual(self.slot_of(self.first).status, Statuses.DECLINED)

    def test_a_slot_submitted_while_being_settled_stays_submitted(self):
        slot = self.slot_of(self.first)

        def submitted_meanwhile(user, instance):
            # The holder's submission commits between the slot being read
            # and its update.
            models.StepContribution.objects.filter(pk=slot.pk).update(
                status=Statuses.SUBMITTED
            )
            return False

        with mock.patch.object(
            contributions, "may_contribute", side_effect=submitted_meanwhile
        ):
            changed = contributions.sync_role_slots(self.first, self.proposal)
        self.assertFalse(changed)
        self.assertEqual(self.slot_of(self.first).status, Statuses.SUBMITTED)

    def test_settling_locks_the_proposal_before_any_slot(self):
        order = []
        real_sync = contributions.sync_role_slots

        def sync(user, proposal):
            order.append("slots")
            return real_sync(user, proposal)

        real_select = models.Proposal.objects.select_for_update

        def select_for_update(*args, **kwargs):
            order.append("proposal")
            return real_select(*args, **kwargs)

        with (
            mock.patch.object(contributions, "sync_role_slots", side_effect=sync),
            mock.patch.object(
                models.Proposal.objects,
                "select_for_update",
                side_effect=select_for_update,
            ),
        ):
            workflow_service.settle_evaluator_slots(self.proposal, self.first)
        self.assertEqual(order[:2], ["proposal", "slots"])

    def test_the_last_open_slot_withdrawn_completes_the_step(self):
        response = self.submit_contribution(self.second, "accepted")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.step_state()[0], WorkflowStepInstanceStatuses.ACTIVE)

        self.revoke_first()
        self.assertEqual(
            self.step_state(),
            (WorkflowStepInstanceStatuses.COMPLETED, WorkflowStepOutcomes.FEASIBLE),
        )

    def test_a_holder_revoked_behind_the_api_cannot_fill_their_slot(self):
        models.ProposalRoleAssignment.objects.filter(user=self.first).update(
            is_lead=False, revoked_at=self.instance.created
        )
        slot = self.slot_of(self.first)
        self.assertEqual(slot.status, Statuses.PENDING)
        self.assertFalse(contributions.user_can_fill(self.first, slot))
        self.assertFalse(contributions.user_can_read(self.first, slot))
        response = self.submit_contribution(self.first, "rejected")
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertFalse(self.can_read(self.first))

    def test_a_recorded_conflict_withdraws_the_slot_and_dismissal_reopens_it(self):
        coi = self.record_conflict()
        self.assertEqual(self.slot_of(self.first).status, Statuses.EXPIRED)
        response = self.submit_contribution(self.first, "rejected")
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

        coi.status = COIStatuses.DISMISSED
        coi.save()
        self.assertEqual(self.slot_of(self.first).status, Statuses.PENDING)
        response = self.submit_contribution(self.first, "accepted")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_recusal_withdraws_the_slot(self):
        # Recused from pending, the only state a conflict is resolved from;
        # unlike a dismissal, nothing reopens the slot afterwards.
        coi = self.record_conflict()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            f"/api/conflicts-of-interest/{coi.uuid.hex}/recuse/",
            {"status": "recused", "review_notes": "Former colleague."},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.slot_of(self.first).status, Statuses.EXPIRED)
        self.assertFalse(self.can_read(self.first))

    def test_a_conflicted_holders_submission_does_not_decide_the_outcome(self):
        response = self.submit_contribution(self.first, "rejected")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.record_conflict()
        # Kept on record, but no longer counted.
        self.assertEqual(self.slot_of(self.first).status, Statuses.SUBMITTED)
        self.assertNotIn(
            self.slot_of(self.first), contributions.counted_contributions(self.instance)
        )
        self.assertIsNone(contributions.derive_step_outcome(self.instance))

        response = self.submit_contribution(self.second, "accepted")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.step_state(),
            (WorkflowStepInstanceStatuses.COMPLETED, WorkflowStepOutcomes.FEASIBLE),
        )

    def test_a_revoked_holders_earlier_submission_still_counts(self):
        # Revoked by hand, with no conflict: what they did while they held
        # the role was theirs to do.
        response = self.submit_contribution(self.first, "rejected")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.revoke_first()
        self.assertEqual(self.slot_of(self.first).status, Statuses.SUBMITTED)
        response = self.submit_contribution(self.second, "accepted")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.step_state(),
            (
                WorkflowStepInstanceStatuses.COMPLETED,
                WorkflowStepOutcomes.INFEASIBLE,
            ),
        )


class ConflictedPanelMemberTest(EvaluatorRoleTestMixin, test.APITestCase):
    """A conflict of interest keeps a panel member off the proposal, as it
    keeps an evaluator role's holder: they neither complete nor assign on the
    panel's steps, contribute to them, nor read the proposal through the
    panel."""

    def setUp(self):
        super().setUp()
        self.step.completer = ResponsibleRoles.PANEL_MEMBER
        self.step.save()
        self.panel_step = self.add_step(
            "panel-assessment",
            StepKinds.ASSESSMENT,
            "Panel assessment",
            2,
            outcome_set=OutcomeSets.DONE,
            contributor_source=f"{ContributorSources.CALL_ROLE}:panel_member",
            completer=ResponsibleRoles.CALL_MANAGER,
        )
        self.reach_assignment_step()

    def record_conflict(self, user):
        profile = factories.ReviewerProfileFactory(user=user)
        return factories.ConflictOfInterestFactory(
            reviewer=profile,
            proposal=self.proposal,
            call=self.call,
            status=COIStatuses.PENDING,
        )

    def reach_panel_step(self):
        self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.workflow_step, "panel-assessment")
        return self.proposal.workflow_step_instances.get(call_step=self.panel_step)

    def submit_contribution(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="submit_contribution"
            ),
            {"step_uuid": self.panel_step.uuid.hex, "outcome": "done"},
            format="json",
        )

    def test_conflicted_member_neither_assigns_completes_nor_reads(self):
        self.record_conflict(self.panel_3)
        self.assertFalse(self.can_read(self.panel_3))
        # Not seeing the proposal, they find nothing to act on.
        response = self.assign(self.panel_1, by=self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.complete("done", user=self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Nor does the permission underneath let them, however they get there.
        self.assertFalse(
            proposal_permissions.user_can_manage_proposal_role(
                self.panel_3, self.proposal, self.role
            )
        )
        self.assertFalse(
            proposal_permissions.user_can_act_on_active_step(
                self.panel_3, self.proposal
            )
        )

        response = self.assign(self.panel_1, by=self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(self.can_read(self.panel_2))

    def test_conflicted_member_gets_no_task_or_mail_for_the_step(self):
        def task_proposals(user):
            return {task.proposal for task in my_tasks.tasks_of(user) if task.proposal}

        self.assertIn(self.proposal, task_proposals(self.panel_3))
        self.assertIn(
            self.panel_3,
            notification_rules._responsible_role_users(self.step, self.proposal),
        )
        self.record_conflict(self.panel_3)
        self.assertNotIn(self.proposal, task_proposals(self.panel_3))
        self.assertNotIn(
            self.panel_3,
            notification_rules._responsible_role_users(self.step, self.proposal),
        )
        self.assertIn(self.proposal, task_proposals(self.panel_2))

    def test_dismissed_conflict_lets_the_member_back(self):
        coi = self.record_conflict(self.panel_3)
        coi.status = COIStatuses.DISMISSED
        coi.save()
        self.assertTrue(self.can_read(self.panel_3))
        response = self.assign(self.panel_3, by=self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_a_conflict_withdraws_the_panel_slot_and_dismissal_reopens_it(self):
        instance = self.reach_panel_step()
        slot = contributions.resolve_contribution(self.panel_3, instance)
        self.assertEqual(slot.status, Statuses.PENDING)

        coi = self.record_conflict(self.panel_3)
        slot.refresh_from_db()
        self.assertEqual(slot.status, Statuses.EXPIRED)
        self.assertFalse(contributions.may_contribute(self.panel_3, instance))
        self.assertFalse(contributions.user_can_fill(self.panel_3, slot))
        response = self.submit_contribution(self.panel_3)
        self.assertIn(
            response.status_code,
            (
                status.HTTP_400_BAD_REQUEST,
                status.HTTP_403_FORBIDDEN,
                status.HTTP_404_NOT_FOUND,
            ),
        )

        coi.status = COIStatuses.DISMISSED
        coi.save()
        slot.refresh_from_db()
        self.assertEqual(slot.status, Statuses.PENDING)
        response = self.submit_contribution(self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_conflicted_members_submission_no_longer_counts(self):
        instance = self.reach_panel_step()
        contributions.resolve_contribution(self.panel_2, instance)
        response = self.submit_contribution(self.panel_3)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        slot = instance.contributions.get(contributor=self.panel_3)
        self.assertIn(slot, contributions.counted_contributions(instance))

        self.record_conflict(self.panel_3)
        slot.refresh_from_db()
        self.assertEqual(slot.status, Statuses.SUBMITTED)
        self.assertNotIn(slot, contributions.counted_contributions(instance))


class CallRoleRevokedTest(EvaluatorRoleTestMixin, test.APITestCase):
    """A panel member who loses the panel role loses the slots it gave them
    on the panel's steps, and the proposal they read through them."""

    def setUp(self):
        super().setUp()
        self.panel_step = self.add_step(
            "panel-assessment",
            StepKinds.ASSESSMENT,
            "Panel assessment",
            2,
            outcome_set=OutcomeSets.DONE,
            contributor_source=f"{ContributorSources.CALL_ROLE}:panel_member",
            completer=ResponsibleRoles.CALL_MANAGER,
        )
        self.reach_assignment_step()
        self.assign(self.panel_1, is_lead=True)
        self.assign(self.panel_2)
        self.assertEqual(self.complete("done").status_code, status.HTTP_200_OK)
        self.instance = self.proposal.workflow_step_instances.get(
            call_step=self.panel_step
        )

    def panel_role(self, user):
        return UserRole.objects.get(
            user=user,
            is_active=True,
            role__name=RoleEnum.CALL_PANEL_MEMBER,
            object_id=self.call.pk,
        )

    def test_revoking_the_panel_role_lapses_the_open_slot(self):
        slot = contributions.resolve_contribution(self.panel_3, self.instance)
        self.assertEqual(slot.status, Statuses.PENDING)
        self.assertTrue(self.can_read(self.panel_3))

        self.panel_role(self.panel_3).revoke()
        slot.refresh_from_db()
        self.assertEqual(slot.status, Statuses.EXPIRED)
        self.assertFalse(self.can_read(self.panel_3))
        self.assertNotIn(
            self.proposal.pk,
            set(managers.get_contributed_proposals(self.panel_3)),
        )

    def test_a_submitted_slot_no_longer_opens_the_proposal(self):
        contributions.resolve_contribution(self.panel_3, self.instance)
        self.client.force_authenticate(self.panel_3)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="submit_contribution"
            ),
            {"step_uuid": self.panel_step.uuid.hex, "outcome": "done"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.panel_role(self.panel_3).revoke()
        slot = self.instance.contributions.get(contributor=self.panel_3)
        self.assertEqual(slot.status, Statuses.SUBMITTED)
        self.assertFalse(self.can_read(self.panel_3))

    def test_an_expired_panel_role_lapses_the_slot(self):
        slot = contributions.resolve_contribution(self.panel_3, self.instance)
        role = self.panel_role(self.panel_3)
        role.expiration_time = timezone.now() - timedelta(minutes=1)
        role.save(update_fields=["expiration_time"])
        permission_tasks.check_expired_permissions()
        slot.refresh_from_db()
        self.assertEqual(slot.status, Statuses.EXPIRED)
        self.assertFalse(self.can_read(self.panel_3))

    def test_the_role_given_back_reopens_the_slot(self):
        slot = contributions.resolve_contribution(self.panel_3, self.instance)
        self.panel_role(self.panel_3).revoke()
        self.call.add_user(self.panel_3, CallRole.PANEL_MEMBER)
        workflow_service.settle_evaluator_slots(self.proposal, self.panel_3)
        slot.refresh_from_db()
        self.assertEqual(slot.status, Statuses.PENDING)
