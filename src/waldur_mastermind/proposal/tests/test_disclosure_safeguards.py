"""What the evaluation's tools tell, and to whom: a role's candidate search
looks people up without listing the user base, a ranking import names no
proposal its importer does not rank, a round's history stays with the call
team, an offering manager with a conflict of interest stays off the
proposal, and a confirmation of having none is kept for good."""

import csv
import io

from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Feed
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import contributions, models, permissions, utils
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_contribution_delegation import (
    OfferingTeamDelegationBase,
)
from waldur_mastermind.proposal.tests.test_proposal_roles import (
    EvaluatorRoleTestMixin,
)
from waldur_mastermind.proposal.tests.test_round_ranking import (
    RANKINGS_URL,
    RankingTestBase,
)
from waldur_mastermind.proposal.tests.test_step_contributions import (
    PerOfferingAssessmentBase,
)

EVENTS_URL = "http://testserver" + reverse("event-list")


class AnyUserCandidateLookupTest(EvaluatorRoleTestMixin, test.APITestCase):
    """For a role anyone may hold, the candidate search looks someone up: a
    few letters at least, a handful of rows, and email only to staff."""

    def setUp(self):
        super().setUp()
        self.reach_assignment_step()
        self.any_role = self.any_user_role()
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="role_assignment_candidates"
        )

    def candidates(self, query, by=None):
        self.client.force_authenticate(by or self.manager)
        response = self.client.get(
            self.url, {"role_uuid": self.any_role.uuid.hex, "query": query}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_a_short_query_lists_nobody(self):
        structure_factories.UserFactory(first_name="Ab")
        self.assertEqual(self.candidates("a"), [])
        self.assertEqual(self.candidates("ab"), [])

    def test_the_call_manager_reads_no_address_nor_searches_one_in_part(self):
        outsider = structure_factories.UserFactory(
            first_name="Quentinella", email="quentinella.private@example.org"
        )

        rows = self.candidates("quentinella")
        self.assertEqual([row["uuid"] for row in rows], [outsider.uuid.hex])
        self.assertIsNone(rows[0]["email"])
        self.assertEqual(self.candidates("private@example"), [])

    def test_staff_read_the_address(self):
        staff = structure_factories.UserFactory(is_staff=True)
        outsider = structure_factories.UserFactory(first_name="Quentinella")

        rows = self.candidates("quentinella", by=staff)

        self.assertEqual(rows[0]["email"], outsider.email)

    def test_a_handful_of_rows_at_most(self):
        for _ in range(15):
            structure_factories.UserFactory(first_name="Bartholomew")

        self.assertEqual(len(self.candidates("bartholomew")), 10)


class RankingImportNamesNothingTest(RankingTestBase):
    UNKNOWN = "no such proposal to rank here"

    def import_rows(self, rows, user=None):
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        self.client.force_authenticate(user or self.call_manager)
        return self.client.post(
            RANKINGS_URL + "import_csv/",
            {
                "round": self.round.uuid.hex,
                "file": SimpleUploadedFile(
                    "ranking.csv", buffer.getvalue().encode(), "text/csv"
                ),
            },
            format="multipart",
        )

    def test_a_proposal_not_rankable_is_refused_like_an_unknown_one(self):
        draft = factories.ProposalFactory(
            round=self.round, state=ProposalStates.DRAFT, name="Secret Draft Name"
        )

        known = self.import_rows([{"proposal_uuid": draft.uuid.hex, "rank": "1"}])
        unknown = self.import_rows([{"proposal_uuid": "f" * 32, "rank": "1"}])

        self.assertEqual(known.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("Secret Draft Name", str(known.data))
        self.assertEqual(known.json(), unknown.json())
        self.assertIn(self.UNKNOWN, str(known.data))

    def test_a_row_without_a_rank_is_skipped_unread(self):
        response = self.import_rows(
            [
                {"proposal_uuid": "f" * 32, "rank": ""},
                {"proposal_uuid": self.first.uuid.hex, "rank": "1"},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_ranking_a_proposal_of_another_round_reads_as_unknown(self):
        other = factories.ProposalFactory(state=ProposalStates.IN_REVIEW)

        response = self.rank(other, 1)
        self.client.force_authenticate(self.call_manager)
        unknown = self.client.post(
            RANKINGS_URL,
            {"round": self.round.uuid.hex, "proposal": "f" * 32, "rank": 1},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.json(), unknown.json())


class RoundAndProposalEventsFeedTest(RankingTestBase):
    """A round's history is filed on its call for the call team; a
    proposal's on the proposal; neither on the organisation's feed."""

    def feeds_of(self, event_type, model):
        return Feed.objects.filter(
            event__event_type=event_type,
            content_type=ContentType.objects.get_for_model(model),
        )

    def read_call(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(
            EVENTS_URL,
            {
                "scope": factories.CallFactory.get_public_url(self.call),
                "event_type": EventType.ROUND_CLOSED,
            },
        )
        if response.status_code == status.HTTP_400_BAD_REQUEST:
            return []
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_round_events_are_filed_on_the_call_for_its_team(self):
        utils.emit_round_event(
            self.round,
            "{round_name} of call {call_name} has been closed.",
            EventType.ROUND_CLOSED,
        )

        self.assertFalse(self.feeds_of(EventType.ROUND_CLOSED, Customer).exists())
        self.assertTrue(
            self.feeds_of(EventType.ROUND_CLOSED, models.Call)
            .filter(object_id=self.call.pk)
            .exists()
        )
        self.assertEqual(len(self.read_call(self.call_manager)), 1)
        self.assertEqual(self.read_call(self.reviewer), [])
        self.assertIsNotNone(permissions.calls_hiding_round_events_from(self.reviewer))

    def test_proposal_events_stay_off_the_organisations_feed(self):
        utils.withdraw_proposal(self.first, self.first.created_by, "No longer needed.")

        self.assertTrue(
            self.feeds_of(EventType.PROPOSAL_CANCELED, models.Proposal).exists()
        )
        self.assertFalse(self.feeds_of(EventType.PROPOSAL_CANCELED, Customer).exists())


class ConflictedOfferingManagerTest(PerOfferingAssessmentBase):
    """A conflict of interest recorded with the proposal keeps an offering's
    manager off its assessment, as it keeps a reviewer."""

    def setUp(self):
        super().setUp()
        profile = factories.ReviewerProfileFactory(user=self.manager_a)
        factories.ConflictOfInterestFactory(
            reviewer=profile, proposal=self.proposal, call=self.call
        )

    def test_the_manager_neither_acts_nor_fills_the_offerings_slot(self):
        self.assertNotIn(
            self.manager_a.pk,
            set(
                permissions.offering_manager_user_ids_for_proposal(
                    self.proposal, self.call_step
                )
            ),
        )
        self.assertFalse(contributions.may_contribute(self.manager_a, self.instance))
        self.assertFalse(
            contributions.fillable_contributions(self.manager_a, self.instance).exists()
        )

    def test_the_other_offerings_manager_still_does(self):
        self.assertTrue(contributions.may_contribute(self.manager_b, self.instance))
        self.assertTrue(
            contributions.fillable_contributions(self.manager_b, self.instance).exists()
        )


class CoiConfirmationRecordTest(OfferingTeamDelegationBase, test.APITestCase):
    """Each confirmation of having no conflict of interest is kept: who gave
    it, when, and which declaration -- handing the slot on keeps it."""

    def setUp(self):
        super().setUp()
        self.call_step.requires_coi_confirmation = True
        self.call_step.save(update_fields=["requires_coi_confirmation"])

    def test_a_confirmation_outlives_the_delegation(self):
        self.handed_over()
        response = self.accept(self.reviewer, self.slot, confirm_no_conflict=True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        [record] = models.StepContributionCoiConfirmation.objects.filter(
            contribution=self.slot
        )
        self.assertEqual(record.user, self.reviewer)
        self.assertEqual(record.declaration_source, "platform")
        self.assertEqual(record.declaration_text, "")

        # Taken back by the provider: the delegate's confirmation stands.
        self.delegate(self.manager_a, self.slot, None)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.coi_declaration_text, "")
        self.assertTrue(
            models.StepContributionCoiConfirmation.objects.filter(
                pk=record.pk, user=self.reviewer
            ).exists()
        )

    def test_the_wording_is_told_apart_from_the_standard_one(self):
        self.call_step.coi_declaration_text = "No ties to the applicants."
        self.call_step.save(update_fields=["coi_declaration_text"])
        self.handed_over()
        self.accept(self.reviewer, self.slot, confirm_no_conflict=True)

        record = models.StepContributionCoiConfirmation.objects.get(
            contribution=self.slot
        )
        self.assertEqual(record.declaration_source, "step")
        self.assertEqual(record.declaration_text, "No ties to the applicants.")

    def test_nothing_is_recorded_where_nothing_is_asked(self):
        self.call_step.requires_coi_confirmation = False
        self.call_step.save(update_fields=["requires_coi_confirmation"])
        self.handed_over()
        self.accept(self.reviewer, self.slot)

        self.assertFalse(models.StepContributionCoiConfirmation.objects.exists())
