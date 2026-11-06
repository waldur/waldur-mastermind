"""Handing a contribution slot to someone else.

A provider hands its technical assessment of a proposal to one of its
offering's team; a domain panel chair hands the consolidated report to one
of the proposal's rapporteurs. The person it is handed to accepts or
declines it, and fills it alone while it stands; its holders keep reading
it, and whoever loses their standing loses the slot with it.
"""

from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.test import override_settings
from rest_framework import status, test

from waldur_core.core.models import Notification
from waldur_core.permissions.fixtures import OfferingRole, ProposalRole
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import call_transfer, models, my_tasks, tasks, utils
from waldur_mastermind.proposal.enums import (
    ChangeRequestResolutions,
    ContributionOutcomes,
    ContributorSources,
    DelegationTargets,
    OutcomeSets,
    ProposalStates,
    ResponsibleRoles,
    StepContributionStatuses,
    StepKinds,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_conditional_and_parallel_steps import (
    ParallelStageBase,
)
from waldur_mastermind.proposal.tests.test_proposal_roles import (
    EvaluatorRoleTestMixin,
)
from waldur_mastermind.proposal.tests.test_step_contributions import (
    PerOfferingAssessmentBase,
)

Statuses = StepContributionStatuses
MY_TASKS_URL = "http://testserver/api/proposal-my-tasks/"


class DelegationActionsMixin:
    def delegate(self, by, contribution, user):
        self.client.force_authenticate(by)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="delegate_contribution"
            ),
            {
                "contribution_uuid": contribution.uuid.hex,
                "user_uuid": user.uuid.hex if user else None,
            },
            format="json",
        )

    def accept(self, user, contribution, **extra):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="accept_contribution"
            ),
            {"contribution_uuid": contribution.uuid.hex, **extra},
            format="json",
        )

    def decline(self, user, contribution):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="decline_contribution"
            ),
            {"contribution_uuid": contribution.uuid.hex},
            format="json",
        )

    def listed(self, user, contribution):
        """The caller's view of ``contribution`` in the proposal's list, or
        None when it is not listed to them."""
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(self.proposal, action="contributions")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return next(
            (row for row in response.data if row["uuid"] == contribution.uuid.hex),
            None,
        )

    def tasks(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(MY_TASKS_URL, {"page_size": 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {row["key"]: row["needed"] for row in response.data}


class OfferingTeamDelegationBase(DelegationActionsMixin, PerOfferingAssessmentBase):
    """The technical assessment of offering A, handed by its provider (the
    centre's representative) to a technical reviewer on its team."""

    def setUp(self):
        super().setUp()
        self.call_step.delegable_to = DelegationTargets.OFFERING_TEAM
        self.call_step.save(update_fields=["delegable_to"])
        self.reviewer = structure_factories.UserFactory()
        self.offering_a.add_user(self.reviewer, OfferingRole.MANAGER)
        self.slot = self._slot(self.requested_a)

    def handed_over(self):
        response = self.delegate(self.manager_a, self.slot, self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        return response


class OfferingTeamDelegationTest(OfferingTeamDelegationBase, test.APITestCase):
    def test_provider_hands_the_slot_to_a_member_of_its_team(self):
        response = self.handed_over()
        self.assertEqual(self.slot.delegate, self.reviewer)
        self.assertEqual(self.slot.delegated_by, self.manager_a)
        self.assertIsNotNone(self.slot.delegated_at)
        self.assertEqual(self.slot.status, Statuses.PENDING)
        self.assertEqual(response.data["delegate_uuid"], self.reviewer.uuid.hex)

        row = self.listed(self.reviewer, self.slot)
        self.assertTrue(row["can_accept"])
        self.assertFalse(row["can_fill"])
        self.assertFalse(row["can_delegate"])

    def test_people_off_the_slots_offering_cannot_take_it(self):
        stranger = structure_factories.UserFactory()
        for user in (stranger, self.manager_b, self.call_manager):
            response = self.delegate(self.manager_a, self.slot, user)
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, user.username
            )
            self.assertEqual(
                response.data["user_uuid"],
                ["This user cannot take the contribution over."],
            )

    def test_an_unknown_user_gets_the_answer_an_ineligible_one_gets(self):
        ghost = structure_factories.UserFactory()
        ghost_uuid = ghost.uuid
        ghost.delete()
        self.client.force_authenticate(self.manager_a)
        response = self.client.post(
            self._url("delegate_contribution"),
            {"contribution_uuid": self.slot.uuid.hex, "user_uuid": ghost_uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["user_uuid"],
            ["This user cannot take the contribution over."],
        )

    def test_applicant_team_and_conflicted_team_members_cannot_take_it(self):
        on_team = structure_factories.UserFactory()
        self.offering_a.add_user(on_team, OfferingRole.MANAGER)
        self.proposal.add_user(on_team, ProposalRole.MEMBER)
        response = self.delegate(self.manager_a, self.slot, on_team)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.reviewer),
            proposal=self.proposal,
        )
        response = self.delegate(self.manager_a, self.slot, self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_offerings_provider_cannot_hand_it_on(self):
        response = self.delegate(self.manager_b, self.slot, self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_step_without_delegation_hands_nothing_on(self):
        self.call_step.delegable_to = DelegationTargets.NONE
        self.call_step.save(update_fields=["delegable_to"])
        response = self.delegate(self.manager_a, self.slot, self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_slot_is_filled_by_the_delegate_alone_once_accepted(self):
        self.handed_over()
        # Not before accepting it: nor by its team meanwhile.
        for user in (self.reviewer, self.manager_a):
            response = self._answer(user, self.slot)
            self.assertEqual(
                response.status_code, status.HTTP_403_FORBIDDEN, user.username
            )

        response = self.accept(self.reviewer, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, Statuses.ACCEPTED)

        self.assertEqual(
            self._answer(self.manager_a, self.slot).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        response = self._answer(self.reviewer, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._submit(
            self.manager_a, self.slot, ContributionOutcomes.ACCEPTED
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self._submit(self.reviewer, self.slot, ContributionOutcomes.ACCEPTED)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, Statuses.SUBMITTED)
        self.assertEqual(self.slot.contributor, self.reviewer)
        self.assertEqual(self.slot.delegate, self.reviewer)

        # The team still reads it; another offering's provider does not.
        row = self.listed(self.manager_a, self.slot)
        self.assertIsNotNone(row)
        self.assertFalse(row["can_fill"])
        self.assertIsNone(self.listed(self.manager_b, self.slot))

    def test_only_the_delegate_accepts(self):
        self.handed_over()
        for user in (self.manager_a, self.manager_b):
            response = self.accept(user, self.slot)
            self.assertEqual(
                response.status_code, status.HTTP_404_NOT_FOUND, user.username
            )

    def test_declining_returns_the_slot_to_the_team(self):
        self.handed_over()
        self.assertEqual(
            self.accept(self.reviewer, self.slot).status_code, status.HTTP_200_OK
        )
        self._answer(self.reviewer, self.slot, "Draft")
        response = self.decline(self.reviewer, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        self.assertIsNone(self.slot.delegate)
        self.assertEqual(self.slot.status, Statuses.PENDING)
        # What was written stays; the team fills it again.
        completion = self.slot.get_checklist_completion(self.checklist)
        self.assertEqual(completion.answers.count(), 1)
        response = self._answer(self.manager_a, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_taking_it_back(self):
        self.handed_over()
        response = self.delegate(self.manager_a, self.slot, None)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        self.assertIsNone(self.slot.delegate)
        self.assertIsNone(self.listed(self.reviewer, self.slot)["delegate_uuid"])
        self.assertEqual(
            self._answer(self.manager_a, self.slot).status_code, status.HTTP_200_OK
        )

    def test_the_delegate_does_not_hand_it_on(self):
        self.handed_over()
        other = structure_factories.UserFactory()
        self.offering_a.add_user(other, OfferingRole.MANAGER)
        response = self.delegate(self.reviewer, self.slot, other)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_call_managers_and_staff_hand_it_on_and_take_it_back(self):
        for user in (self.call_manager, self.fixture.staff):
            response = self.delegate(user, self.slot, self.reviewer)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            response = self.delegate(user, self.slot, None)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_losing_the_offering_role_returns_the_slot(self):
        self.handed_over()
        self.accept(self.reviewer, self.slot)
        self.offering_a.remove_user(self.reviewer, OfferingRole.MANAGER)
        self.assertEqual(
            self.accept(self.reviewer, self.slot).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        # Off the offering's team, they no longer read the proposal at all.
        self.assertEqual(
            self._answer(self.reviewer, self.slot).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        response = self._answer(self.manager_a, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(self.listed(self.manager_a, self.slot)["delegate_uuid"])

    def test_a_conflict_recorded_later_returns_the_slot(self):
        self.handed_over()
        self.accept(self.reviewer, self.slot)
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.reviewer),
            proposal=self.proposal,
        )
        # The slot is the team's again, as before it was handed on.
        self.assertIsNone(self.listed(self.manager_a, self.slot)["delegate_uuid"])
        self.assertFalse(self.listed(self.reviewer, self.slot)["can_accept"])
        self.assertEqual(
            self._answer(self.manager_a, self.slot).status_code, status.HTTP_200_OK
        )

    def test_the_task_list_follows_the_delegation(self):
        key = f"contribution:{self.slot.uuid.hex}"
        self.assertEqual(self.tasks(self.manager_a).get(key), my_tasks.Needs.CONTRIBUTE)
        self.handed_over()
        self.assertNotIn(key, self.tasks(self.manager_a))
        self.assertEqual(
            self.tasks(self.reviewer).get(key), my_tasks.Needs.ACCEPT_DELEGATION
        )
        self.accept(self.reviewer, self.slot)
        self.assertEqual(self.tasks(self.reviewer).get(key), my_tasks.Needs.CONTRIBUTE)

    def test_the_applicant_does_not_learn_who_took_it_over(self):
        self.call_step.applicant_visible = True
        self.call_step.save(update_fields=["applicant_visible"])
        self.call.reviews_visible_to_submitters = True
        self.call.reviewer_identity_visible_to_submitters = False
        self.call.save()
        self.handed_over()
        self.accept(self.reviewer, self.slot)
        self._answer(self.reviewer, self.slot)
        self._submit(self.reviewer, self.slot, ContributionOutcomes.ACCEPTED)
        row = self.listed(self.proposal.created_by, self.slot)
        self.assertIsNotNone(row)
        self.assertIsNone(row["delegate_uuid"])
        self.assertIsNone(row["delegate_full_name"])
        self.assertIsNone(row["contributor_uuid"])
        self.assertFalse(row["can_delegate"])
        self.assertEqual(row["delegation_candidates"], [])

    def test_candidates_are_the_offerings_managers(self):
        row = self.listed(self.manager_a, self.slot)
        self.assertTrue(row["can_delegate"])
        self.assertEqual(
            {c["uuid"] for c in row["delegation_candidates"]},
            {self.manager_a.uuid.hex, self.reviewer.uuid.hex},
        )
        self.assertEqual(self.listed(self.manager_b, self.slot), None)


class TechnicalReviewerRoleTest(
    DelegationActionsMixin, PerOfferingAssessmentBase, test.APITestCase
):
    """A centre names its technical reviewers with an offering role of its
    own that grants nothing in the marketplace. Handed a slot, the reviewer
    reads the proposal only once they accept it, and only while they hold
    the role."""

    def setUp(self):
        super().setUp()
        self.call_step.delegable_to = DelegationTargets.OFFERING_TEAM
        self.call_step.save(update_fields=["delegable_to"])
        self.role = Role.objects.create(
            name="Technical reviewer",
            content_type=ContentType.objects.get_for_model(marketplace_models.Offering),
        )
        self.reviewer = structure_factories.UserFactory()
        self.offering_a.add_user(self.reviewer, self.role)
        self.slot = self._slot(self.requested_a)

    def reads(self, user, proposal=None):
        self.client.force_authenticate(user)
        response = self.client.get(
            factories.ProposalFactory.get_url(proposal or self.proposal)
        )
        return response.status_code == status.HTTP_200_OK

    def hand_over_and_accept(self):
        response = self.delegate(self.manager_a, self.slot, self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self.accept(self.reviewer, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_role_without_permissions_on_the_offering_qualifies(self):
        row = self.listed(self.manager_a, self.slot)
        self.assertIn(
            self.reviewer.uuid.hex, {c["uuid"] for c in row["delegation_candidates"]}
        )
        response = self.delegate(self.manager_a, self.slot, self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_proposal_opens_only_once_accepted(self):
        self.assertFalse(self.reads(self.reviewer))
        self.delegate(self.manager_a, self.slot, self.reviewer)
        self.assertFalse(self.reads(self.reviewer))
        # They answer from their task list, without reading the proposal.
        self.assertEqual(
            self.tasks(self.reviewer).get(f"contribution:{self.slot.uuid.hex}"),
            my_tasks.Needs.ACCEPT_DELEGATION,
        )
        response = self.accept(self.reviewer, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(self.reads(self.reviewer))
        response = self._answer(self.reviewer, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        response = self._submit(self.reviewer, self.slot, ContributionOutcomes.ACCEPTED)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Still theirs to read once written, while they hold the role.
        self.assertTrue(self.reads(self.reviewer))

    def test_it_stays_readable_but_not_writable_once_decided(self):
        self.hand_over_and_accept()
        self._answer(self.reviewer, self.slot)
        response = self._submit(self.reviewer, self.slot, ContributionOutcomes.ACCEPTED)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # The step and the proposal decided: what they wrote is still theirs
        # to read, as the record of their work; nothing is theirs to change.
        models.ProposalWorkflowStepInstance.objects.filter(
            id=self.slot.proposal_step_id
        ).update(status=WorkflowStepInstanceStatuses.COMPLETED)
        models.Proposal.objects.filter(id=self.proposal.id).update(
            state=ProposalStates.ACCEPTED
        )
        self.assertTrue(self.reads(self.reviewer))
        response = self._answer(self.reviewer, self.slot)
        self.assertGreaterEqual(response.status_code, 400, response.data)

    def test_a_pending_delegate_declines_without_reading_the_proposal(self):
        self.delegate(self.manager_a, self.slot, self.reviewer)
        response = self.decline(self.reviewer, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        self.assertIsNone(self.slot.delegate)

    def test_nobody_else_answers_through_the_back_door(self):
        self.delegate(self.manager_a, self.slot, self.reviewer)
        stranger = structure_factories.UserFactory()
        for user in (stranger, self.manager_b):
            self.assertEqual(
                self.accept(user, self.slot).status_code, status.HTTP_404_NOT_FOUND
            )
            self.assertEqual(
                self.decline(user, self.slot).status_code, status.HTTP_404_NOT_FOUND
            )

    def test_revoking_the_role_or_taking_it_back_closes_the_proposal(self):
        self.hand_over_and_accept()
        self.offering_a.remove_user(self.reviewer, self.role)
        self.assertFalse(self.reads(self.reviewer))
        self.offering_a.add_user(self.reviewer, self.role)
        self.assertTrue(self.reads(self.reviewer))
        self.delegate(self.manager_a, self.slot, None)
        self.assertFalse(self.reads(self.reviewer))

    def test_a_conflict_closes_the_proposal(self):
        self.hand_over_and_accept()
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.reviewer),
            proposal=self.proposal,
        )
        self.assertFalse(self.reads(self.reviewer))

    def test_other_proposals_of_the_call_stay_closed(self):
        self.hand_over_and_accept()
        other = factories.ProposalFactory(
            round=self.fixture.round, state=self.proposal.state
        )
        factories.RequestedResourceFactory(
            proposal=other, requested_offering=self.requested_a
        )
        self.assertTrue(self.reads(self.reviewer))
        self.assertFalse(self.reads(self.reviewer, other))
        self.client.force_authenticate(self.reviewer)
        response = self.client.get(factories.ProposalFactory.get_list_url())
        self.assertEqual(
            [row["uuid"] for row in response.data], [self.proposal.uuid.hex]
        )

    def test_the_delegate_reads_only_its_own_slot(self):
        self.hand_over_and_accept()
        self.assertIsNotNone(self.listed(self.reviewer, self.slot))
        self.assertIsNone(self.listed(self.reviewer, self._slot(self.requested_b)))

    def test_the_marketplace_stays_closed_to_the_role(self):
        self.hand_over_and_accept()
        self.client.force_authenticate(self.reviewer)
        response = self.client.get(
            marketplace_factories.OfferingFactory.get_url(self.offering_a)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingTeamDelegationMailTest(OfferingTeamDelegationBase, test.APITestCase):
    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.contribution_delegated", defaults={"enabled": True}
        )
        mail.outbox = []

    def test_each_side_is_told(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.handed_over()
        self.assertEqual([m.to for m in mail.outbox], [[self.reviewer.email]])
        self.assertIn("handed to you", mail.outbox[0].subject)

        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.decline(self.reviewer, self.slot)
        self.assertEqual([m.to for m in mail.outbox], [[self.manager_a.email]])
        self.assertIn("declined", mail.outbox[0].subject)

        self.handed_over()
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.accept(self.reviewer, self.slot)
        self.assertEqual([m.to for m in mail.outbox], [[self.manager_a.email]])
        self.assertIn("accepted", mail.outbox[0].subject)

        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.delegate(self.manager_a, self.slot, None)
        self.assertEqual([m.to for m in mail.outbox], [[self.reviewer.email]])
        self.assertIn("no longer yours", mail.outbox[0].subject)

    def test_the_mail_can_be_switched_off(self):
        Notification.objects.filter(key="proposal.contribution_delegated").update(
            enabled=False
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.handed_over()
        self.assertEqual(mail.outbox, [])


class ReopenedDelegatedSlotTest(OfferingTeamDelegationBase, test.APITestCase):
    """A slot its delegate wrote, reopened by the call team, goes back to the
    delegate, who revises it; its holders keep reading the request."""

    def setUp(self):
        super().setUp()
        Notification.objects.update_or_create(
            key="proposal.workflow_step_event", defaults={"enabled": True}
        )
        self.handed_over()
        self.assertEqual(
            self.accept(self.reviewer, self.slot).status_code, status.HTTP_200_OK
        )
        self._answer(self.reviewer, self.slot)
        response = self._submit(self.reviewer, self.slot, ContributionOutcomes.ACCEPTED)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def reopen(self):
        self.client.force_authenticate(self.call_manager)
        mail.outbox = []
        with (
            mock.patch.object(
                tasks.notify_change_request,
                "delay",
                side_effect=tasks.notify_change_request,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(
                self._url("reopen_contribution"),
                {"contribution_uuid": self.slot.uuid.hex, "message": "Recheck it."},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()

    def readers(self):
        readers = set()
        for user in (self.reviewer, self.manager_a, self.manager_b):
            self.client.force_authenticate(user)
            response = self.client.get(self._url("change_requests"))
            if response.status_code == status.HTTP_200_OK and response.data:
                readers.add(user)
        return readers

    def test_the_delegate_revises_it_and_is_told(self):
        self.reopen()
        self.assertEqual(self.slot.status, Statuses.ACCEPTED)
        self.assertEqual(self.slot.delegate, self.reviewer)
        recipients = {address for message in mail.outbox for address in message.to}
        self.assertIn(self.reviewer.email, recipients)
        self.assertIn("Recheck it.", mail.outbox[0].body)

        key = f"contribution:{self.slot.uuid.hex}"
        self.assertEqual(self.tasks(self.reviewer).get(key), my_tasks.Needs.CONTRIBUTE)
        self.assertNotIn(key, self.tasks(self.manager_a))

        # The holders read it but do not fill it; the delegate does.
        self.assertEqual(
            self._answer(self.manager_a, self.slot).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        response = self._submit(self.reviewer, self.slot, ContributionOutcomes.ACCEPTED)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        change_request = models.ProposalChangeRequest.objects.get()
        self.assertEqual(change_request.resolution, ChangeRequestResolutions.SUBMITTED)

    def test_the_mail_links_the_offering_teams_view(self):
        self.reopen()
        body = next(m.body for m in mail.outbox if self.reviewer.email in m.to)
        self.assertIn(
            tasks.offering_team_proposal_url(
                self.slot.proposal_step.proposal.uuid,
                self.offering_a.customer.uuid,
            ),
            body,
        )

    def test_the_delegate_and_the_holders_read_the_request(self):
        self.reopen()
        self.assertEqual(self.readers(), {self.reviewer, self.manager_a})

    def test_a_delegate_no_longer_eligible_leaves_it_to_the_holders(self):
        self.offering_a.remove_user(self.reviewer, OfferingRole.MANAGER)
        self.reopen()
        self.assertEqual(self.slot.status, Statuses.PENDING)
        recipients = {address for message in mail.outbox for address in message.to}
        self.assertNotIn(self.reviewer.email, recipients)
        self.assertIn(self.manager_a.email, recipients)
        response = self._answer(self.manager_a, self.slot)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class PanelChairReportDelegationTest(
    DelegationActionsMixin, EvaluatorRoleTestMixin, test.APITestCase
):
    """The domain panel chair owns the consolidated report and may hand it
    to one of the proposal's rapporteurs."""

    def setUp(self):
        super().setUp()
        self.chair_role = models.CallProposalRole.objects.create(
            call=self.call,
            key="domain_panel_chair",
            name="Domain Panel Chair",
            max_holders=1,
        )
        self.step.assigns_roles.set([self.role, self.chair_role])
        self.report = self.add_step(
            "consolidated-report",
            StepKinds.ASSESSMENT,
            "Consolidated report",
            2,
            outcome_set=OutcomeSets.DONE,
            contributor_source=f"{ContributorSources.PROPOSAL_ROLE}:{self.chair_role.pk}",
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.chair_role,
            min_contributions=1,
            max_contributions=1,
            delegable_to=f"{ContributorSources.PROPOSAL_ROLE}:{self.role.pk}",
        )
        self.chair = self.panel_3
        self.reach_assignment_step()
        for user, role in (
            (self.chair, self.chair_role),
            (self.panel_1, self.role),
            (self.panel_2, self.role),
        ):
            response = self.assign(user, role=role, is_lead=user == self.panel_1)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, response.data
            )
        response = self.complete("done")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.workflow_step, "consolidated-report")
        self.instance = self.proposal.current_step
        self.slot = self.instance.contributions.get()

    def submit_report(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.ProposalFactory.get_url(
                self.proposal, action="submit_contribution"
            ),
            {"contribution_uuid": self.slot.uuid.hex},
            format="json",
        )

    def test_the_chair_owns_the_slot_from_the_start(self):
        self.assertEqual(self.slot.contributor, self.chair)
        row = self.listed(self.chair, self.slot)
        self.assertTrue(row["can_fill"])
        self.assertTrue(row["can_delegate"])
        self.assertEqual(
            {c["uuid"] for c in row["delegation_candidates"]},
            {self.panel_1.uuid.hex, self.panel_2.uuid.hex},
        )

    def test_a_rapporteur_writes_it_in_the_chairs_place(self):
        response = self.delegate(self.chair, self.slot, self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.accept(self.panel_2, self.slot).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.submit_report(self.chair).status_code, status.HTTP_403_FORBIDDEN
        )
        response = self.submit_report(self.panel_2)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, Statuses.SUBMITTED)
        self.assertEqual(self.slot.contributor, self.chair)
        self.assertEqual(self.slot.delegate, self.panel_2)
        # The chair reads it and completes the step.
        self.assertIsNotNone(self.listed(self.chair, self.slot))
        response = self.complete("done", user=self.chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_before_accepting_the_rapporteur_sees_the_slot_but_not_its_answers(self):
        self.delegate(self.chair, self.slot, self.panel_2)
        row = self.listed(self.panel_2, self.slot)
        self.assertTrue(row["can_accept"])
        self.assertFalse(row["can_fill"])
        self.client.force_authenticate(self.panel_2)
        url = factories.ProposalFactory.get_url(self.proposal, action="step-checklist")
        response = self.client.get(
            url,
            {
                "step_uuid": self.report.uuid.hex,
                "contribution_uuid": self.slot.uuid.hex,
            },
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_400_BAD_REQUEST),
        )
        # Nor through the step's answers listing.
        response = self.client.get(
            factories.ProposalFactory.get_url(
                self.proposal, action="step-checklist-responses"
            ),
            {"step_uuid": self.report.uuid.hex},
        )
        self.assertIn(
            response.status_code, (status.HTTP_200_OK, status.HTTP_403_FORBIDDEN)
        )
        if response.status_code == status.HTTP_200_OK:
            self.assertNotIn(self.slot.uuid.hex, [row["uuid"] for row in response.data])
        # The other rapporteur does not see it at all.
        self.assertIsNone(self.listed(self.panel_1, self.slot))

    def test_the_slot_goes_only_to_the_named_roles_holders(self):
        outsider = self.panel_member()
        for user in (outsider, self.chair):
            response = self.delegate(self.chair, self.slot, user)
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, user.username
            )

    def test_other_rapporteurs_neither_hand_it_on_nor_fill_it(self):
        self.delegate(self.chair, self.slot, self.panel_2)
        self.accept(self.panel_2, self.slot)
        response = self.delegate(self.panel_1, self.slot, self.panel_1)
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertEqual(
            self.submit_report(self.panel_1).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_revoking_the_rapporteur_returns_the_slot_to_the_chair(self):
        self.delegate(self.chair, self.slot, self.panel_2)
        self.accept(self.panel_2, self.slot)
        assignment = models.ProposalRoleAssignment.objects.get(
            proposal=self.proposal, role=self.role, user=self.panel_2
        )
        response = self.revoke(assignment.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.submit_report(self.panel_2).status_code, status.HTTP_403_FORBIDDEN
        )
        response = self.submit_report(self.chair)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        self.assertIsNone(self.slot.delegate)

    def test_a_call_manager_reassigns_it(self):
        self.delegate(self.chair, self.slot, self.panel_2)
        response = self.delegate(self.manager, self.slot, self.panel_1)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.delegate, self.panel_1)
        self.assertEqual(self.slot.delegated_by, self.manager)

    def test_coi_confirmation_is_asked_where_the_step_asks_it(self):
        self.report.requires_coi_confirmation = True
        self.report.save(update_fields=["requires_coi_confirmation"])
        self.delegate(self.chair, self.slot, self.panel_2)
        self.assertEqual(
            self.accept(self.panel_2, self.slot).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        response = self.accept(self.panel_2, self.slot, confirm_no_conflict=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_the_delegate_reads_and_keeps_the_steps_declaration(self):
        self.report.requires_coi_confirmation = True
        self.report.coi_declaration_text = "I have no ties to the applicants."
        self.report.save()
        self.delegate(self.chair, self.slot, self.panel_2)
        self.assertEqual(
            self.listed(self.panel_2, self.slot)["coi_declaration_text"],
            "I have no ties to the applicants.",
        )
        self.client.force_authenticate(self.panel_2)
        rows = self.client.get(MY_TASKS_URL, {"page_size": 100}).data
        task = next(
            row for row in rows if row["key"] == f"contribution:{self.slot.uuid.hex}"
        )
        self.assertEqual(
            task["coi_declaration_text"], "I have no ties to the applicants."
        )
        response = self.accept(self.panel_2, self.slot, confirm_no_conflict=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # A later edit of the wording leaves what was confirmed alone.
        self.report.coi_declaration_text = "Another wording."
        self.report.save()
        self.slot.refresh_from_db()
        self.assertEqual(
            self.slot.coi_declaration_text, "I have no ties to the applicants."
        )
        self.assertEqual(
            self.listed(self.chair, self.slot)["coi_declaration_text"],
            "I have no ties to the applicants.",
        )

        # Handed to someone else, nothing is confirmed yet.
        self.delegate(self.manager, self.slot, self.panel_1)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.coi_declaration_text, "")


class DelegableToConfigTest(EvaluatorRoleTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.call.state = "draft"
        self.call.save()
        self.chair_role = models.CallProposalRole.objects.create(
            call=self.call, key="chair", name="Chair", max_holders=1
        )
        self.report = self.add_step(
            "report",
            StepKinds.ASSESSMENT,
            "Report",
            2,
            outcome_set=OutcomeSets.DONE,
            contributor_source=f"{ContributorSources.PROPOSAL_ROLE}:{self.chair_role.pk}",
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.chair_role,
        )
        self.tech = models.CallWorkflowStep.objects.get(
            call=self.call, key="technical_assessment"
        )
        self.tech.contributor_source = ContributorSources.REQUESTED_OFFERING_TEAM
        self.tech.per_requested_offering = True
        self.tech.outcome_set = OutcomeSets.PER_OFFERING
        self.tech.save()
        self.client.force_authenticate(self.fixture.staff)

    def patch(self, step, value):
        return self.client.patch(
            factories.CallWorkflowStepFactory.get_url(self.call, step),
            {"delegable_to": value},
            format="json",
        )

    def test_valid_targets_are_accepted(self):
        for step, value in (
            (self.tech, DelegationTargets.OFFERING_TEAM),
            (self.report, f"proposal_role:{self.role.pk}"),
            (self.report, f"proposal_role:{self.role.uuid.hex}"),
            (self.report, ""),
        ):
            response = self.patch(step, value)
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assertEqual(response.data["delegable_to"], value)
            self.assertEqual(
                response.data["delegable_proposal_role_uuid"],
                self.role.uuid.hex if value.startswith("proposal_role:") else None,
            )

    def test_invalid_targets_are_refused(self):
        other_call_role = models.CallProposalRole.objects.create(
            call=factories.CallFactory(), key="x", name="X"
        )
        for step, value in (
            (self.report, DelegationTargets.OFFERING_TEAM),
            (self.tech, f"proposal_role:{self.role.pk}"),
            (self.report, f"proposal_role:{self.chair_role.pk}"),
            (self.report, f"proposal_role:{other_call_role.pk}"),
            (self.report, "proposal_role:"),
            (self.report, "everyone"),
        ):
            response = self.patch(step, value)
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, (step.key, value)
            )
            self.assertIn("delegable_to", response.data)

    def test_the_role_travels_by_key(self):
        self.report.delegable_to = f"proposal_role:{self.role.pk}"
        self.report.save(update_fields=["delegable_to"])
        self.tech.delegable_to = DelegationTargets.OFFERING_TEAM
        self.tech.save(update_fields=["delegable_to"])
        document, _ = call_transfer.export_call(self.call)
        exported = {s["key"]: s["delegable_to"] for s in document["workflow_steps"]}
        self.assertEqual(exported["report"], "proposal_role:rapporteur")
        self.assertEqual(exported["technical_assessment"], "offering_team")

        imported, _, warnings = call_transfer.import_call(
            document, self.call.manager, self.fixture.staff
        )
        copied = utils.duplicate_call(self.call, "Copy", self.fixture.staff)
        for call in (imported, copied):
            rapporteur = call.proposal_roles.get(key="rapporteur")
            self.assertEqual(
                call.workflow_steps.get(key="report").delegable_to,
                f"proposal_role:{rapporteur.pk}",
            )
            self.assertEqual(
                call.workflow_steps.get(key="technical_assessment").delegable_to,
                DelegationTargets.OFFERING_TEAM,
            )


class DelegationInParallelStageTest(DelegationActionsMixin, ParallelStageBase):
    """A technical assessment slot handed on while a panel review runs beside
    the assessment: the delegate's task is the slot's, and the stage moves
    on only once both steps have ended."""

    def setUp(self):
        super().setUp()
        self.technical.delegable_to = DelegationTargets.OFFERING_TEAM
        self.technical.save(update_fields=["delegable_to"])
        self.reviewer = structure_factories.UserFactory()
        self.requested_a.offering.add_user(self.reviewer, OfferingRole.MANAGER)
        self.slot = self._instance("technical_assessment").contributions.get(
            requested_offering=self.requested_a
        )

    def test_the_delegate_works_one_step_of_the_stage(self):
        response = self.delegate(self.manager_a, self.slot, self.reviewer)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.accept(self.reviewer, self.slot).status_code, status.HTTP_200_OK
        )
        key = f"contribution:{self.slot.uuid.hex}"
        tasks = self.tasks(self.reviewer)
        self.assertEqual(tasks.get(key), my_tasks.Needs.CONTRIBUTE)
        panel_uuid = self._instance("panel_review").uuid.hex
        self.assertFalse(any(panel_uuid in task_key for task_key in tasks))

        self._assess(
            "technical_assessment",
            self.requested_a,
            self.reviewer,
            ContributionOutcomes.ACCEPTED,
        )
        self._assess(
            "technical_assessment",
            self.requested_b,
            self.manager_b,
            ContributionOutcomes.ACCEPTED,
        )
        self.assertEqual(
            self._instance("technical_assessment").status,
            WorkflowStepInstanceStatuses.COMPLETED,
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.current_step.step, "panel_review")
        self.assertEqual(
            self._instance("allocation_decision").status,
            WorkflowStepInstanceStatuses.PENDING,
        )
        self.assertNotIn(key, self.tasks(self.reviewer))
