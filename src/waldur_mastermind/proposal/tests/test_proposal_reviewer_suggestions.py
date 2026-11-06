"""Reviewers suggested for a proposal by its rapporteurs.

The call runs: administrative check, the rapporteurs' reviewer suggestions,
expert review and the allocation decision. Two rapporteurs hold an evaluator
role on the proposal; the call manager turns their suggestions into pool
invitations and assigns those who accepted to the expert review.
"""

import json
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import exceptions as drf_exceptions
from rest_framework import status, test

from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import (
    call_transfer,
    models,
    proposal_roles,
    reviewer_membership,
    reviewer_pool,
    reviewer_suggestions,
    tasks,
    utils,
)
from waldur_mastermind.proposal.enums import (
    COIStatuses,
    COITypes,
    OutcomeSets,
    ProposalReviewerSuggestionStatuses,
    ProposalRoleEligibility,
    ResponsibleRoles,
    ReviewerPoolInvitationStatuses,
    StepKinds,
)
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_call_steps import CallStepsTestMixin

Statuses = ProposalReviewerSuggestionStatuses

# Refused: forbidden, or not found when the proposal is hidden from the user.
REFUSED = (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND)


def invitations(**filters):
    """The call's pool entries made by an invitation -- not those its
    reviewer role holders have, who are in the pool as well (see
    reviewer_membership)."""
    return models.CallReviewerPool.objects.exclude(
        override_reason=reviewer_membership.GRANTED_REASON
    ).filter(**filters)


class ReviewerSuggestionsTestMixin(CallStepsTestMixin):
    def setUp(self):
        super().setUp()
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.manager = self.fixture.call_manager
        self.applicant = self.fixture.proposal_creator
        self.team_member = structure_factories.UserFactory()
        self.proposal.add_user(self.team_member, ProposalRole.MEMBER)
        self.bystander = structure_factories.UserFactory()

        models.CallWorkflowStep.objects.filter(
            call=self.call, key__in=["technical_assessment", "panel_review"]
        ).update(is_enabled=False)
        self.review_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="expert_review"
        )
        self.review_step.is_enabled = True
        self.review_step.save()

        self.role = models.CallProposalRole.objects.create(
            call=self.call,
            key="rapporteur",
            name="Rapporteur",
            eligible_from=ProposalRoleEligibility.ANY_USER,
            max_holders=2,
        )
        self.suggestion_step = self.add_step(
            "reviewer-suggestions",
            StepKinds.ASSESSMENT,
            "Reviewer suggestions",
            1,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.PROPOSAL_ROLE,
            completer_proposal_role=self.role,
            contributor_source=f"proposal_role:{self.role.pk}",
            collects_reviewer_suggestions=True,
        )
        self.rapporteur_1 = structure_factories.UserFactory()
        self.rapporteur_2 = structure_factories.UserFactory()
        self.assignments = {}
        for user, is_lead in ((self.rapporteur_1, True), (self.rapporteur_2, False)):
            self.assignments[user.pk] = models.ProposalRoleAssignment.objects.create(
                proposal=self.proposal,
                role=self.role,
                user=user,
                is_lead=is_lead,
                coi_confirmation_required=False,
            )

        structure_factories.NotificationFactory(key="proposal.reviewer_invitation")
        self.expert = structure_factories.UserFactory(email="expert@example.com")
        self.expert_profile = factories.ReviewerProfileFactory(
            user=self.expert, is_published=True
        )

    def url(self, action):
        return factories.ProposalFactory.get_url(self.proposal, action)

    def reach_suggestion_step(self):
        self.submit()
        self.complete_ok("eligible")
        self.assertEqual(self.proposal.workflow_step, "reviewer-suggestions")

    def complete_ok(self, outcome):
        response = self.complete(outcome)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def suggest(self, user, **payload):
        self.client.force_authenticate(user)
        return self.client.post(
            self.url("my_reviewer_suggestions"), payload, format="json"
        )

    def suggest_ok(self, user, **payload):
        response = self.suggest(user, **payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data

    def suggested(self, user=None):
        self.client.force_authenticate(user or self.manager)
        response = self.client.get(self.url("suggested_reviewers"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def invite(self, suggestion_uuids, user=None):
        self.client.force_authenticate(user or self.manager)
        return self.client.post(
            self.url("invite_suggested_reviewers"),
            {"suggestion_uuids": suggestion_uuids},
            format="json",
        )


class SuggestingTest(ReviewerSuggestionsTestMixin, test.APITestCase):
    def test_rapporteur_suggests_a_user_and_an_email(self):
        self.reach_suggestion_step()
        by_user = self.suggest_ok(
            self.rapporteur_1,
            suggested_user=self.expert.uuid.hex,
            note="HPC scheduling",
        )
        by_email = self.suggest_ok(
            self.rapporteur_1, suggested_email="Outside@Example.com"
        )
        self.assertEqual(by_user["status"], Statuses.PENDING)
        self.assertEqual(by_user["suggested_user_name"], self.expert.full_name)
        self.assertEqual(by_email["suggested_email"], "outside@example.com")
        suggestion = models.ProposalReviewerSuggestion.objects.get(uuid=by_user["uuid"])
        self.assertEqual(suggestion.contribution.contributor, self.rapporteur_1)
        self.assertEqual(
            suggestion.contribution.proposal_step.call_step, self.suggestion_step
        )

        self.client.force_authenticate(self.rapporteur_1)
        response = self.client.get(self.url("my_reviewer_suggestions"))
        self.assertEqual(len(response.data), 2)
        # A rapporteur lists their own suggestions only.
        self.client.force_authenticate(self.rapporteur_2)
        response = self.client.get(self.url("my_reviewer_suggestions"))
        self.assertEqual(response.data, [])

    def test_only_while_the_step_is_active(self):
        self.submit()
        response = self.suggest(self.rapporteur_1, suggested_user=self.expert.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.reach_suggestion_step_from_admin_check()
        self.complete_ok("done")
        response = self.suggest(self.rapporteur_1, suggested_user=self.expert.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def reach_suggestion_step_from_admin_check(self):
        self.complete_ok("eligible")
        self.assertEqual(self.proposal.workflow_step, "reviewer-suggestions")

    def test_other_users_cannot_suggest(self):
        self.reach_suggestion_step()
        for user in (self.bystander, self.applicant, self.team_member, self.manager):
            response = self.suggest(user, suggested_user=self.expert.uuid.hex)
            self.assertIn(response.status_code, REFUSED, (user, response.data))
        self.assertFalse(models.ProposalReviewerSuggestion.objects.exists())

    def suggesting_step_state(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self.url("workflow_states"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return next(
            row for row in response.data if row["step"] == "reviewer-suggestions"
        )

    def test_the_page_is_told_who_may_suggest(self):
        self.reach_suggestion_step()
        self.assertTrue(
            self.suggesting_step_state(self.rapporteur_1)["can_suggest_reviewers"]
        )
        for user in (self.manager, self.applicant):
            self.assertFalse(self.suggesting_step_state(user)["can_suggest_reviewers"])
        # Asking opens no slot.
        self.assertFalse(
            models.StepContribution.objects.filter(contributor=self.manager).exists()
        )

        contribution = models.StepContribution.objects.get(
            proposal_step__call_step=self.suggestion_step,
            contributor=self.rapporteur_1,
        )
        self.client.force_authenticate(self.rapporteur_1)
        response = self.client.post(
            self.url("submit_contribution"),
            {"contribution_uuid": contribution.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        # Submitted: the backend refuses more suggestions, and says so first.
        self.assertFalse(
            self.suggesting_step_state(self.rapporteur_1)["can_suggest_reviewers"]
        )
        response = self.suggest(self.rapporteur_1, suggested_user=self.expert.uuid.hex)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            self.suggesting_step_state(self.rapporteur_2)["can_suggest_reviewers"]
        )

    def test_revoked_holder_cannot_suggest(self):
        self.reach_suggestion_step()
        assignment = self.assignments[self.rapporteur_2.pk]
        assignment.revoked_at = timezone.now()
        assignment.save()
        response = self.suggest(self.rapporteur_2, suggested_user=self.expert.uuid.hex)
        self.assertIn(response.status_code, REFUSED)
        self.assertFalse(models.ProposalReviewerSuggestion.objects.exists())

    def test_holder_with_standing_conflict_cannot_suggest(self):
        self.reach_suggestion_step()
        models.ConflictOfInterest.objects.create(
            reviewer=factories.ReviewerProfileFactory(user=self.rapporteur_2),
            call=self.call,
            proposal=self.proposal,
            coi_type=COITypes.INST_SAME,
            status=COIStatuses.PENDING,
        )
        response = self.suggest(self.rapporteur_2, suggested_user=self.expert.uuid.hex)
        self.assertIn(response.status_code, REFUSED)
        self.assertFalse(models.ProposalReviewerSuggestion.objects.exists())

    def test_refused_suggestions(self):
        self.reach_suggestion_step()
        self.suggest_ok(self.rapporteur_1, suggested_user=self.expert.uuid.hex)
        cases = [
            {"suggested_user": self.expert.uuid.hex},  # again
            {"suggested_user": self.rapporteur_1.uuid.hex},  # oneself
            {"suggested_user": self.team_member.uuid.hex},  # applicant team
            {"suggested_email": self.rapporteur_1.email},  # oneself, by address
            {},  # nobody
            {
                "suggested_user": self.expert.uuid.hex,
                "suggested_email": "x@example.com",
            },
        ]
        for payload in cases:
            response = self.suggest(self.rapporteur_1, **payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)

    def test_an_address_is_taken_as_typed_and_its_owner_never_invited(self):
        # Refusing the applicant's address would tell whose it is.
        self.reach_suggestion_step()
        data = self.suggest_ok(self.rapporteur_1, suggested_email=self.applicant.email)
        self.assertIsNone(data["suggested_user"])
        self.assertIsNone(data["suggested_user_name"])
        self.assertNotIn(self.applicant.full_name, str(data))
        response = self.invite([data["uuid"]])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["invited"], 0)
        self.assertEqual(response.data["refused"], 1)

    def test_withdraw_own_pending_suggestion(self):
        self.reach_suggestion_step()
        data = self.suggest_ok(self.rapporteur_1, suggested_email="a@example.com")
        self.client.force_authenticate(self.rapporteur_2)
        response = self.client.post(
            self.url("withdraw_reviewer_suggestion"), {"suggestion_uuid": data["uuid"]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.client.force_authenticate(self.rapporteur_1)
        response = self.client.post(
            self.url("withdraw_reviewer_suggestion"), {"suggestion_uuid": data["uuid"]}
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.ProposalReviewerSuggestion.objects.exists())

    def test_only_a_step_of_evaluator_role_holders_collects_suggestions(self):
        step = models.CallWorkflowStep(
            call=self.call,
            key="x",
            kind=StepKinds.ASSESSMENT,
            name="x",
            order=9,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.CALL_MANAGER,
            collects_reviewer_suggestions=True,
        )
        with self.assertRaises(DjangoValidationError):
            step.clean()


class SuggestionCandidatesTest(ReviewerSuggestionsTestMixin, test.APITestCase):
    """The rapporteur's picker: registered users they may suggest."""

    def setUp(self):
        super().setUp()
        for user in (
            self.expert,
            self.applicant,
            self.team_member,
            self.rapporteur_1,
            self.rapporteur_2,
            self.bystander,
        ):
            user.last_name = "Curie"
            user.save()
        self.affiliation = factories.ReviewerAffiliationFactory(
            reviewer_profile=self.expert_profile,
            organization=None,
            organization_name="Radium Institute",
        )
        # In the call's pool without a published profile.
        self.pool_reviewer = structure_factories.UserFactory(last_name="Curie")
        self.pool_entry = models.CallReviewerPool.objects.create(
            call=self.call,
            reviewer=factories.ReviewerProfileFactory(user=self.pool_reviewer),
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        # A profile not published, nor in the pool: not found.
        self.hidden = structure_factories.UserFactory(last_name="Curie")
        factories.ReviewerProfileFactory(user=self.hidden)
        # The applicant team is never found, with a published profile or not.
        factories.ReviewerProfileFactory(user=self.team_member, is_published=True)

    def search(self, user, query="curie"):
        self.client.force_authenticate(user)
        return self.client.get(
            self.url("reviewer_suggestion_candidates"), {"query": query}
        )

    def search_ok(self, user, query="curie"):
        response = self.search(user, query)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_holder_finds_pool_and_profile_reviewers(self):
        self.reach_suggestion_step()
        data = self.search_ok(self.rapporteur_1)
        by_uuid = {row["uuid"]: row for row in data}
        self.assertEqual(
            set(by_uuid), {self.expert.uuid.hex, self.pool_reviewer.uuid.hex}
        )
        expert = by_uuid[self.expert.uuid.hex]
        self.assertEqual(expert["full_name"], self.expert.full_name)
        self.assertEqual(expert["affiliation"], "Radium Institute")
        self.assertTrue(expert["has_published_profile"])
        self.assertIsNone(expert["pool_status"])
        self.assertFalse(expert["has_conflict"])
        pooled = by_uuid[self.pool_reviewer.uuid.hex]
        self.assertFalse(pooled["has_published_profile"])
        self.assertEqual(pooled["pool_status"], Statuses.ACCEPTED)

    def test_matches_every_word_on_name_username_or_email(self):
        self.reach_suggestion_step()
        query = f"{self.expert.first_name} curie"
        data = self.search_ok(self.rapporteur_1, query)
        self.assertEqual([row["uuid"] for row in data], [self.expert.uuid.hex])
        data = self.search_ok(self.rapporteur_1, "EXPERT@example")
        self.assertEqual([row["uuid"] for row in data], [self.expert.uuid.hex])
        data = self.search_ok(self.rapporteur_1, self.pool_reviewer.username)
        self.assertEqual([row["uuid"] for row in data], [self.pool_reviewer.uuid.hex])

    def test_no_email_is_returned(self):
        self.reach_suggestion_step()
        response = self.search(self.rapporteur_1)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for row in response.data:
            self.assertNotIn("email", row)
        body = response.content.decode()
        self.assertNotIn(self.expert.email, body)
        self.assertNotIn(self.pool_reviewer.email, body)

    def test_short_query_finds_nobody(self):
        self.reach_suggestion_step()
        self.assertEqual(self.search_ok(self.rapporteur_1, ""), [])
        self.assertEqual(self.search_ok(self.rapporteur_1, "cu"), [])

    def test_conflict_is_flagged_not_hidden(self):
        self.reach_suggestion_step()
        models.ConflictOfInterest.objects.create(
            reviewer=self.expert_profile,
            call=self.call,
            proposal=self.proposal,
            coi_type=COITypes.INST_SAME,
            status=COIStatuses.PENDING,
        )
        data = self.search_ok(self.rapporteur_1)
        by_uuid = {row["uuid"]: row for row in data}
        self.assertTrue(by_uuid[self.expert.uuid.hex]["has_conflict"])
        self.assertFalse(by_uuid[self.pool_reviewer.uuid.hex]["has_conflict"])

    def test_evaluator_role_holders_are_left_out(self):
        self.reach_suggestion_step()
        factories.ReviewerProfileFactory(user=self.rapporteur_2, is_published=True)
        data = self.search_ok(self.rapporteur_1)
        self.assertNotIn(self.rapporteur_2.uuid.hex, {row["uuid"] for row in data})

    def test_caller_is_left_out(self):
        self.reach_suggestion_step()
        factories.ReviewerProfileFactory(user=self.rapporteur_1, is_published=True)
        data = self.search_ok(self.rapporteur_1)
        self.assertNotIn(self.rapporteur_1.uuid.hex, {row["uuid"] for row in data})

    def test_others_are_refused(self):
        self.reach_suggestion_step()
        assignment = self.assignments[self.rapporteur_2.pk]
        assignment.revoked_at = timezone.now()
        assignment.save()
        for user in (
            self.bystander,
            self.applicant,
            self.team_member,
            self.manager,
            self.rapporteur_2,
        ):
            response = self.search(user)
            self.assertIn(response.status_code, REFUSED, user)

    def test_refused_while_the_step_is_not_active(self):
        self.submit()
        response = self.search(self.rapporteur_1)
        self.assertIn(response.status_code, REFUSED)

    def test_results_are_capped(self):
        self.reach_suggestion_step()
        for _ in range(reviewer_suggestions.CANDIDATES_LIMIT + 3):
            factories.ReviewerProfileFactory(
                user=structure_factories.UserFactory(last_name="Curie"),
                is_published=True,
            )
        data = self.search_ok(self.rapporteur_1)
        self.assertEqual(len(data), reviewer_suggestions.CANDIDATES_LIMIT)

    def count_queries(self):
        self.client.force_authenticate(self.rapporteur_1)
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(
                self.url("reviewer_suggestion_candidates"), {"query": "curie"}
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return len(response.data), len(context.captured_queries)

    def test_query_count_is_flat(self):
        self.reach_suggestion_step()
        # The first read of a setting stores its default; measure after it.
        self.count_queries()
        few, before = self.count_queries()
        for index in range(6):
            user = structure_factories.UserFactory(last_name="Curie")
            profile = factories.ReviewerProfileFactory(user=user, is_published=True)
            factories.ReviewerAffiliationFactory(reviewer_profile=profile)
            if index % 2:
                models.CallReviewerPool.objects.create(call=self.call, reviewer=profile)
                models.ConflictOfInterest.objects.create(
                    reviewer=profile,
                    call=self.call,
                    proposal=self.proposal,
                    coi_type=COITypes.INST_SAME,
                    status=COIStatuses.PENDING,
                )
        many, after = self.count_queries()
        self.assertEqual(many, few + 6)
        self.assertEqual(before, after)


@override_settings(task_always_eager=True)
class CallManagerViewTest(ReviewerSuggestionsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_suggestion_step()
        self.first = self.suggest_ok(
            self.rapporteur_1, suggested_user=self.expert.uuid.hex, note="Scheduling"
        )
        # The second rapporteur names the same person by address.
        self.second = self.suggest_ok(
            self.rapporteur_2, suggested_email="EXPERT@example.com", note="Networks"
        )
        self.outsider = self.suggest_ok(
            self.rapporteur_2, suggested_email="new.reviewer@example.com"
        )

    def candidate(self, data, email):
        return next(c for c in data if c["email"].lower() == email)

    def test_duplicates_are_merged_with_their_suggesters(self):
        data = self.suggested()
        self.assertEqual(len(data), 2)
        expert = self.candidate(data, "expert@example.com")
        self.assertEqual(expert["suggested_user_uuid"], self.expert.uuid.hex)
        self.assertEqual(
            {s["suggester_name"] for s in expert["suggestions"]},
            {self.rapporteur_1.full_name, self.rapporteur_2.full_name},
        )
        self.assertEqual(
            {s["note"] for s in expert["suggestions"]}, {"Scheduling", "Networks"}
        )
        self.assertEqual(expert["status"], Statuses.PENDING)
        self.assertFalse(expert["has_conflict"])
        self.assertFalse(expert["is_applicant_excluded"])

    def test_the_completer_reads_no_address_but_what_was_typed(self):
        data = self.suggested(self.rapporteur_1)
        picked = next(
            c for c in data if c["suggested_user_uuid"] == self.expert.uuid.hex
        )
        self.assertEqual(picked["email"], "")
        typed = self.candidate(data, "expert@example.com")
        # Nor is the address typed matched to its user.
        self.assertIsNone(typed["suggested_user_uuid"])
        self.assertIsNone(typed["suggested_user_name"])
        self.assertFalse(typed["already_in_pool"])
        self.assertEqual(str(data).count(self.expert.uuid.hex), 2)
        self.assertNotIn(self.expert.email, str(picked))

    def test_conflicts_of_interest_are_flagged(self):
        models.ConflictOfInterest.objects.create(
            reviewer=self.expert_profile,
            call=self.call,
            proposal=self.proposal,
            coi_type=COITypes.INST_SAME,
            status=COIStatuses.PENDING,
        )
        expert = self.candidate(self.suggested(), "expert@example.com")
        self.assertTrue(expert["has_conflict"])
        self.assertEqual(len(expert["conflicts"]), 1)

    def test_who_sees_the_suggestions(self):
        self.assertEqual(len(self.suggested(self.fixture.staff)), 2)
        # The holders of the evaluator role completing the step, who read
        # the address typed as standing for itself.
        self.assertEqual(len(self.suggested(self.rapporteur_1)), 3)
        for user in (
            self.applicant,
            self.team_member,
            self.bystander,
        ):
            self.client.force_authenticate(user)
            response = self.client.get(self.url("suggested_reviewers"))
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                user,
            )

    def test_bulk_invitation_goes_through_the_pool(self):
        mail.outbox = []
        response = self.invite([self.first["uuid"], self.outsider["uuid"]])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["invited"], 2)
        entries = invitations(call=self.call)
        self.assertEqual(entries.count(), 2)
        expert_entry = entries.get(invited_email__iexact="expert@example.com")
        self.assertEqual(expert_entry.reviewer, self.expert_profile)
        self.assertEqual(expert_entry.invited_by, self.manager)
        self.assertIsNotNone(expert_entry.invitation_expires_at)
        self.assertEqual(
            sorted(m.to[0] for m in mail.outbox),
            ["expert@example.com", "new.reviewer@example.com"],
        )
        # Both rapporteurs' suggestions of the expert follow the invitation.
        for data in (self.first, self.second):
            suggestion = models.ProposalReviewerSuggestion.objects.get(
                uuid=data["uuid"]
            )
            self.assertEqual(suggestion.pool_entry, expert_entry)
            self.assertEqual(suggestion.current_status, Statuses.INVITED)

        # Inviting again sends nothing.
        mail.outbox = []
        response = self.invite([self.second["uuid"]])
        self.assertEqual(response.data["invited"], 0)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(invitations().count(), 2)

    def test_someone_already_in_the_pool_is_linked_not_invited_again(self):
        entry = models.CallReviewerPool.objects.create(
            call=self.call,
            reviewer=self.expert_profile,
            invited_by=self.manager,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        mail.outbox = []
        response = self.invite([self.first["uuid"]])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["invited"], 0)
        self.assertEqual(mail.outbox, [])
        expert = self.candidate(self.suggested(), "expert@example.com")
        self.assertEqual(expert["pool_entry_uuid"], entry.uuid.hex)
        self.assertEqual(expert["status"], Statuses.ACCEPTED)

    def test_only_the_call_managers_invite(self):
        for user in (self.rapporteur_1, self.applicant, self.bystander):
            response = self.invite([self.first["uuid"]], user=user)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                user,
            )
        self.assertFalse(invitations().exists())

    def test_suggestions_of_another_proposal_are_refused(self):
        response = self.invite([factories.ProposalFactory().uuid.hex])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_follows_the_invitation(self):
        self.invite([self.first["uuid"], self.outsider["uuid"]])
        entry = invitations().get(invited_email__iexact="expert@example.com")

        # The expert accepts through the pool invitation.
        self.client.force_authenticate(self.expert)
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(entry, "accept")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.candidate(self.suggested(), "expert@example.com")["status"],
            Statuses.ACCEPTED,
        )

        outsider_entry = invitations().get(invited_email="new.reviewer@example.com")
        outsider_entry.invitation_expires_at = timezone.now() - timedelta(hours=1)
        outsider_entry.save()
        self.assertEqual(
            self.candidate(self.suggested(), "new.reviewer@example.com")["status"],
            Statuses.EXPIRED,
        )
        tasks.mark_expired_reviewer_pool_invitations()
        self.assertEqual(
            self.candidate(self.suggested(), "new.reviewer@example.com")["status"],
            Statuses.EXPIRED,
        )

        outsider_entry.invitation_status = ReviewerPoolInvitationStatuses.DECLINED
        outsider_entry.save()
        self.assertEqual(
            self.candidate(self.suggested(), "new.reviewer@example.com")["status"],
            Statuses.DECLINED,
        )

    def test_discard(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.url("discard_suggested_reviewers"),
            {"suggestion_uuids": [self.outsider["uuid"]]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.candidate(self.suggested(), "new.reviewer@example.com")["status"],
            Statuses.DISCARDED,
        )
        # Not once invited.
        self.invite([self.first["uuid"]])
        response = self.client.post(
            self.url("discard_suggested_reviewers"),
            {"suggestion_uuids": [self.first["uuid"]]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invited_suggestion_cannot_be_withdrawn(self):
        self.invite([self.first["uuid"]])
        self.client.force_authenticate(self.rapporteur_1)
        response = self.client.post(
            self.url("withdraw_reviewer_suggestion"),
            {"suggestion_uuid": self.first["uuid"]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_accepted_suggested_reviewer_is_assigned_to_the_review_step(self):
        self.invite([self.first["uuid"]])
        entry = invitations().get(invited_email__iexact="expert@example.com")
        self.client.force_authenticate(self.expert)
        self.client.post(factories.CallReviewerPoolFactory.get_url(entry, "accept"))
        expert = self.candidate(self.suggested(), "expert@example.com")
        self.assertEqual(expert["status"], Statuses.ACCEPTED)

        self.complete_ok("done")
        self.assertEqual(self.proposal.workflow_step, "expert_review")
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="create-manual-assignment"
            ),
            {
                "reviewer_pool_entry_uuid": expert["pool_entry_uuid"],
                "proposal_uuids": [self.proposal.uuid.hex],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["items_created"], 1)
        # The manual assignment waits in a draft batch, and the row says so.
        expert = self.candidate(self.suggested(), "expert@example.com")
        self.assertEqual(expert["assignment_batch_status"], "draft")
        batch = models.AssignmentBatch.objects.get(uuid=response.data["batch_uuid"])
        batch.send_invitation(user=self.manager)
        expert = self.candidate(self.suggested(), "expert@example.com")
        self.assertEqual(expert["assignment_batch_status"], "sent")
        item = batch.items.get()
        self.client.force_authenticate(self.expert)
        response = self.client.post(
            factories.AssignmentItemFactory.get_url(item) + "accept/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        contribution = models.StepContribution.objects.get(
            review__proposal=self.proposal, contributor=self.expert
        )
        self.assertEqual(contribution.proposal_step.call_step, self.review_step)


@override_settings(task_always_eager=True)
class ApplicantSeesNothingTest(ReviewerSuggestionsTestMixin, test.APITestCase):
    def test_no_trace_on_the_applicant_side(self):
        mail.outbox = []
        self.reach_suggestion_step()
        last_event_id = Event.objects.order_by("-id").values_list("id", flat=True)
        last_event_id = last_event_id.first() or 0
        data = self.suggest_ok(
            self.rapporteur_1,
            suggested_user=self.expert.uuid.hex,
            note="SECRET-EXPERTISE-NOTE",
        )
        self.invite([data["uuid"]])
        markers = [
            "SECRET-EXPERTISE-NOTE",
            self.expert.email,
            self.expert.uuid.hex,
            data["uuid"],
        ]
        self.complete_ok("done")
        urls = [
            factories.ProposalFactory.get_url(self.proposal),
            factories.ProposalFactory.get_list_url(),
            self.url("contributions"),
        ]
        for step in (self.suggestion_step, self.review_step):
            query = f"?step_uuid={step.uuid.hex}"
            urls.append(self.url("step_inputs") + query)
            urls.append(self.url("step-checklist-responses") + query)
        for user in (self.applicant, self.team_member):
            self.client.force_authenticate(user)
            for url in urls:
                response = self.client.get(url)
                body = json.dumps(response.data, default=str)
                for marker in markers:
                    self.assertNotIn(marker, body, (user, url))
            for action in ("suggested_reviewers", "my_reviewer_suggestions"):
                response = self.client.get(self.url(action))
                self.assertIn(response.status_code, REFUSED)
        # Nothing logged since the suggestion names the suggested reviewer.
        events = json.dumps(
            list(
                Event.objects.filter(id__gt=last_event_id).values_list(
                    "message", "context"
                )
            ),
            default=str,
        )
        for marker in markers:
            self.assertNotIn(marker, events)
        applicant_addresses = {self.applicant.email, self.team_member.email}
        for message in mail.outbox:
            self.assertFalse(applicant_addresses & set(message.to), message.subject)


@override_settings(task_always_eager=True)
class WhoReadsTest(ReviewerSuggestionsTestMixin, test.APITestCase):
    """The completer reads every suggestion with the suggested people's
    addresses, so only the holders of an evaluator role, resolved per
    proposal, are given that -- and nobody with a conflict on the proposal."""

    def setUp(self):
        super().setUp()
        self.reach_suggestion_step()
        self.suggest_ok(
            self.rapporteur_1, suggested_user=self.expert.uuid.hex, note="SECRET"
        )

    def assert_refused(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self.url("suggested_reviewers"))
        self.assertIn(response.status_code, REFUSED, user)

    def record_conflict(self, user):
        models.ConflictOfInterest.objects.create(
            reviewer=factories.ReviewerProfileFactory(user=user),
            call=self.call,
            proposal=self.proposal,
            coi_type=COITypes.INST_SAME,
            status=COIStatuses.PENDING,
        )

    def test_a_call_role_completer_does_not_read(self):
        # A step configured before a call role was refused as its completer.
        models.CallWorkflowStep.objects.filter(pk=self.suggestion_step.pk).update(
            completer=ResponsibleRoles.REVIEWER, completer_proposal_role=None
        )
        reviewer = structure_factories.UserFactory()
        self.call.add_user(reviewer, CallRole.REVIEWER)
        self.assert_refused(reviewer)
        self.record_conflict(reviewer)
        self.assert_refused(reviewer)
        # Not even the rapporteurs: they read as the completer's role holders.
        self.assert_refused(self.rapporteur_2)
        self.assertEqual(len(self.suggested()), 1)

    def test_a_revoked_holder_does_not_read(self):
        self.assertEqual(len(self.suggested(self.rapporteur_2)), 1)
        assignment = self.assignments[self.rapporteur_2.pk]
        assignment.revoked_at = timezone.now()
        assignment.save()
        self.assert_refused(self.rapporteur_2)

    def test_a_holder_with_a_standing_conflict_does_not_read(self):
        self.record_conflict(self.rapporteur_2)
        self.assert_refused(self.rapporteur_2)

    def test_a_call_manager_with_a_recorded_conflict_does_not_read(self):
        self.record_conflict(self.manager)
        self.assert_refused(self.manager)
        self.assertEqual(len(self.suggested(self.fixture.staff)), 1)

    def test_support_on_the_applicant_team_does_not_read(self):
        self.team_member.is_support = True
        self.team_member.save(update_fields=["is_support"])
        self.assertFalse(reviewer_suggestions.may_view(self.team_member, self.proposal))
        self.assert_refused(self.team_member)

    def test_staff_on_the_applicant_team_neither_reads_nor_invites(self):
        self.team_member.is_staff = True
        self.team_member.save(update_fields=["is_staff"])
        self.assertFalse(reviewer_suggestions.may_view(self.team_member, self.proposal))
        self.assertFalse(
            reviewer_suggestions.may_invite(self.team_member, self.proposal)
        )
        self.assert_refused(self.team_member)
        [candidate] = self.suggested()
        [suggestion] = candidate["suggestions"]
        response = self.invite([suggestion["uuid"]], user=self.team_member)
        self.assertIn(response.status_code, REFUSED)

    def test_support_with_a_recorded_conflict_does_not_read(self):
        support = structure_factories.UserFactory(is_support=True)
        self.assertTrue(reviewer_suggestions.may_view(support, self.proposal))
        self.record_conflict(support)
        self.assertFalse(reviewer_suggestions.may_view(support, self.proposal))

    def test_a_collecting_step_is_completed_by_an_evaluator_role(self):
        step = models.CallWorkflowStep(
            call=self.call,
            key="x",
            kind=StepKinds.ASSESSMENT,
            name="x",
            order=9,
            outcome_set=OutcomeSets.DONE,
            completer=ResponsibleRoles.REVIEWER,
            contributor_source=f"proposal_role:{self.role.pk}",
            collects_reviewer_suggestions=True,
        )
        with self.assertRaises(DjangoValidationError) as error:
            step.clean()
        self.assertIn("completer", error.exception.message_dict)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call) + "workflow_steps/",
            {
                "kind": StepKinds.ASSESSMENT,
                "name": "Suggestions read by reviewers",
                "outcome_set": OutcomeSets.DONE,
                "completer": ResponsibleRoles.REVIEWER,
                "contributor_source": f"proposal_role:{self.role.pk}",
                "collects_reviewer_suggestions": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("completer", response.data)

    def test_whether_a_step_collects_is_fixed_once_proposals_walk_it(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(
            factories.CallFactory.get_protected_url(self.call)
            + f"workflow_steps/{self.suggestion_step.uuid.hex}/",
            {"collects_reviewer_suggestions": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("collects_reviewer_suggestions", response.data)
        self.suggestion_step.refresh_from_db()
        self.assertTrue(self.suggestion_step.collects_reviewer_suggestions)
        self.assertEqual(len(self.suggested(self.rapporteur_2)), 1)


@override_settings(task_always_eager=True)
class WithdrawTest(ReviewerSuggestionsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_suggestion_step()
        self.data = self.suggest_ok(self.rapporteur_2, suggested_email="a@example.com")

    def withdraw(self):
        self.client.force_authenticate(self.rapporteur_2)
        return self.client.post(
            self.url("withdraw_reviewer_suggestion"),
            {"suggestion_uuid": self.data["uuid"]},
        )

    def test_a_revoked_holder_cannot_take_it_back(self):
        assignment = self.assignments[self.rapporteur_2.pk]
        assignment.revoked_at = timezone.now()
        assignment.save()
        self.assertIn(self.withdraw().status_code, REFUSED)
        self.assertTrue(models.ProposalReviewerSuggestion.objects.exists())

    def test_a_holder_with_a_standing_conflict_cannot_take_it_back(self):
        models.ConflictOfInterest.objects.create(
            reviewer=factories.ReviewerProfileFactory(user=self.rapporteur_2),
            call=self.call,
            proposal=self.proposal,
            coi_type=COITypes.INST_SAME,
            status=COIStatuses.PENDING,
        )
        self.assertIn(self.withdraw().status_code, REFUSED)
        self.assertTrue(models.ProposalReviewerSuggestion.objects.exists())


@override_settings(task_always_eager=True)
class InvitationLifecycleTest(ReviewerSuggestionsTestMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.reach_suggestion_step()

    def my(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self.url("my_reviewer_suggestions"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_a_later_suggestion_of_an_invited_person_follows_the_invitation(self):
        first = self.suggest_ok(self.rapporteur_1, suggested_user=self.expert.uuid.hex)
        self.assertEqual(self.invite([first["uuid"]]).data["invited"], 1)
        entry = invitations().get(call=self.call)

        late = self.suggest_ok(self.rapporteur_2, suggested_email="expert@example.com")
        self.assertEqual(late["status"], Statuses.INVITED)
        self.assertEqual(
            models.ProposalReviewerSuggestion.objects.get(uuid=late["uuid"]).pool_entry,
            entry,
        )
        self.assertEqual(self.my(self.rapporteur_2)[0]["status"], Statuses.INVITED)
        merged = self.suggested()
        self.assertEqual(merged[0]["status"], Statuses.INVITED)
        self.assertEqual(len(merged[0]["suggestions"]), 2)

        # Every row stands where the invitation does: not taken back, not
        # discarded, not invited twice.
        self.client.force_authenticate(self.rapporteur_2)
        response = self.client.post(
            self.url("withdraw_reviewer_suggestion"), {"suggestion_uuid": late["uuid"]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.url("discard_suggested_reviewers"),
            {"suggestion_uuids": [late["uuid"]]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.invite([late["uuid"]])
        self.assertEqual(response.data["invited"], 0)
        self.assertEqual(response.data["results"][0]["outcome"], "refused")
        self.assertEqual(invitations().count(), 1)

    def test_inviting_links_unlinked_suggestions_of_an_invited_person(self):
        first = self.suggest_ok(self.rapporteur_1, suggested_user=self.expert.uuid.hex)
        self.invite([first["uuid"]])
        late = self.suggest_ok(self.rapporteur_2, suggested_email="expert@example.com")
        # Left unlinked, as before suggestions followed the invitation.
        models.ProposalReviewerSuggestion.objects.filter(uuid=late["uuid"]).update(
            pool_entry=None, invited_at=None, invited_by=None
        )
        mail.outbox = []
        response = self.invite([late["uuid"]])
        self.assertEqual(response.data["linked"], 1)
        self.assertEqual(response.data["results"][0]["outcome"], "linked")
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.my(self.rapporteur_2)[0]["status"], Statuses.INVITED)

    def test_an_expired_invitation_is_sent_again(self):
        first = self.suggest_ok(self.rapporteur_1, suggested_user=self.expert.uuid.hex)
        self.invite([first["uuid"]])
        entry = invitations().get(call=self.call)
        token = entry.invitation_token
        entry.invitation_expires_at = timezone.now() - timedelta(days=1)
        entry.save()
        self.assertEqual(self.suggested()[0]["status"], Statuses.EXPIRED)

        mail.outbox = []
        response = self.invite([first["uuid"]])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["resent"], 1)
        self.assertEqual(response.data["results"][0]["outcome"], "resent")
        self.assertEqual([m.to[0] for m in mail.outbox], ["expert@example.com"])
        entry.refresh_from_db()
        self.assertEqual(
            entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )
        self.assertGreater(entry.invitation_expires_at, timezone.now())
        self.assertNotEqual(entry.invitation_token, token)
        self.assertEqual(self.suggested()[0]["status"], Statuses.INVITED)

    def test_a_declined_invitation_is_not_sent_again(self):
        models.CallReviewerPool.objects.create(
            call=self.call,
            reviewer=self.expert_profile,
            invited_email="Expert@Example.com",
            invitation_status=ReviewerPoolInvitationStatuses.DECLINED,
            response_date=timezone.now() - timedelta(days=3),
        )
        first = self.suggest_ok(self.rapporteur_1, suggested_user=self.expert.uuid.hex)
        self.assertTrue(self.suggested()[0]["already_in_pool"])
        mail.outbox = []
        response = self.invite([first["uuid"]])
        result = response.data["results"][0]
        self.assertEqual(result["outcome"], "refused")
        self.assertTrue(
            result["detail"].startswith("Declined the invitation on "), result
        )
        self.assertIn("not re-invited", result["detail"])
        self.assertEqual(response.data["refused"], 1)
        self.assertEqual(mail.outbox, [])
        # Left for the call manager to set aside, not linked silently.
        self.assertIsNone(
            models.ProposalReviewerSuggestion.objects.get(uuid=first["uuid"]).pool_entry
        )
        self.assertEqual(self.suggested()[0]["status"], Statuses.PENDING)

    def test_an_address_invited_concurrently_is_not_a_server_error(self):
        # Another request invited the address between the check and the insert.
        competitor = models.CallReviewerPool.objects.create(
            call=self.call,
            invited_email="new@example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        with (
            mock.patch.object(
                models.CallReviewerPool.objects,
                "create",
                side_effect=IntegrityError("unique_call_invited_email"),
            ),
            mock.patch.object(
                reviewer_pool, "find_pool_entry", return_value=competitor
            ),
        ):
            with self.assertRaises(reviewer_pool.AlreadyInPool) as error:
                reviewer_pool.invite_by_email(
                    self.call, "other@example.com", self.manager
                )
        self.assertEqual(error.exception.entry, competitor)

    def test_an_invitation_made_concurrently_is_linked(self):
        data = self.suggest_ok(self.rapporteur_1, suggested_email="new@example.com")
        competitor = models.CallReviewerPool.objects.create(
            call=self.call,
            invited_email="new@example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        with (
            mock.patch.object(reviewer_pool.PoolIndex, "find", return_value=None),
            mock.patch.object(
                reviewer_pool,
                "invite_by_email",
                side_effect=reviewer_pool.AlreadyInPool(competitor, "exists"),
            ),
        ):
            response = self.invite([data["uuid"]])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["results"][0]["outcome"], "linked")
        self.assertEqual(
            models.ProposalReviewerSuggestion.objects.get(uuid=data["uuid"]).pool_entry,
            competitor,
        )

    def test_the_invitation_email_goes_out_on_commit_only(self):
        data = self.suggest_ok(self.rapporteur_1, suggested_email="new@example.com")

        def on_commit(func, using=None, robust=False):
            transaction.get_connection(using).on_commit(func, robust)

        mail.outbox = []
        with mock.patch.object(transaction, "on_commit", on_commit):
            # Rolled back: nothing is sent.
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                try:
                    with transaction.atomic():
                        reviewer_suggestions.invite(
                            self.proposal, [data["uuid"]], self.manager
                        )
                        raise RuntimeError("rolled back")
                except RuntimeError:
                    pass
            self.assertEqual(callbacks, [])
            self.assertEqual(mail.outbox, [])
            self.assertFalse(invitations().exists())

            # Committed: sent once the transaction commits, not before.
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                reviewer_suggestions.invite(self.proposal, [data["uuid"]], self.manager)
                self.assertEqual(mail.outbox, [])
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
        self.assertEqual([m.to[0] for m in mail.outbox], ["new@example.com"])

    def test_listing_does_not_query_per_suggested_person(self):
        counts = []
        for people in (1, 11):
            models.ProposalReviewerSuggestion.objects.all().delete()
            for index in range(people):
                email = f"person-{people}-{index}@example.com"
                self.suggest_ok(self.rapporteur_1, suggested_email=email)
                self.suggest_ok(self.rapporteur_2, suggested_email=email)
            self.client.force_authenticate(self.manager)
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.url("suggested_reviewers"))
            self.assertEqual(len(response.data), people)
            counts.append(len(queries.captured_queries))
        self.assertEqual(counts[0], counts[1], counts)


class CopiedCallTest(ReviewerSuggestionsTestMixin, test.APITestCase):
    """A copy of the call collects suggestions through the copy's roles."""

    def assert_collects_through_its_own_role(self, new_call):
        step = new_call.workflow_steps.get(key="reviewer-suggestions")
        self.assertTrue(step.collects_reviewer_suggestions)
        role = proposal_roles.resolve_role(new_call, step.contributor_source)
        self.assertIsNotNone(role)
        self.assertEqual(role.key, "rapporteur")

    def test_duplicate(self):
        new_call = utils.duplicate_call(self.call, "copy", self.fixture.staff)
        self.assert_collects_through_its_own_role(new_call)

    def test_export_and_import(self):
        data, _ = call_transfer.export_call(self.call)
        step = next(
            s for s in data["workflow_steps"] if s["key"] == "reviewer-suggestions"
        )
        self.assertEqual(step["contributor_source"], "proposal_role:rapporteur")
        new_call, _, _ = call_transfer.import_call(
            data, self.call.manager, self.fixture.staff
        )
        self.assert_collects_through_its_own_role(new_call)

    def test_import_refuses_a_role_the_document_does_not_define(self):
        data, _ = call_transfer.export_call(self.call)
        step = next(
            s for s in data["workflow_steps"] if s["key"] == "reviewer-suggestions"
        )
        # Named by the identity it had on the exporting portal.
        step["contributor_source"] = f"proposal_role:{self.role.pk}"
        with self.assertRaises(drf_exceptions.ValidationError) as error:
            call_transfer.import_call(data, self.call.manager, self.fixture.staff)
        self.assertIn("contributor_source", json.dumps(error.exception.detail))
