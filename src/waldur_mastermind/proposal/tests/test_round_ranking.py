"""A round's ranking, its meeting notes, and completing a step for the whole round."""

import csv
import io
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import media_access, models, rankings, workflow_service
from waldur_mastermind.proposal.enums import (
    DEFAULT_CALL_STEPS,
    CallStates,
    COIStatuses,
    NotificationRuleRecipients,
    NotificationRuleTriggers,
    ProposalStates,
    RoundLifecycleStates,
    WorkflowStepInstanceStatuses,
)
from waldur_mastermind.proposal.tests import factories

RANKINGS_URL = "http://testserver" + reverse("proposal-ranking-list")
NOTES_URL = "http://testserver" + reverse("round-meeting-note-list")
STEP_EVENT = "waldur_mastermind.proposal.tasks.notify_workflow_step_event.delay"


def ranking_url(ranking, action=None):
    url = "http://testserver" + reverse(
        "proposal-ranking-detail", kwargs={"uuid": ranking.uuid.hex}
    )
    return url + f"{action}/" if action else url


class RankingTestBase(test.APITestCase):
    def setUp(self):
        for permission in (
            PermissionEnum.LIST_CALLS,
            PermissionEnum.LIST_PROPOSALS,
            PermissionEnum.LIST_ROUNDS,
            PermissionEnum.UPDATE_CALL,
        ):
            CallRole.MANAGER.add_permission(permission)
        self.call = factories.CallFactory(state=CallStates.ACTIVE)
        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        self.panel_member = structure_factories.UserFactory()
        self.call.add_user(self.panel_member, CallRole.PANEL_MEMBER)
        self.reviewer = structure_factories.UserFactory()
        self.call.add_user(self.reviewer, CallRole.REVIEWER)
        now = timezone.now()
        self.round = factories.RoundFactory(
            call=self.call,
            start_time=now - timedelta(days=30),
            cutoff_time=now - timedelta(minutes=1),
        )
        self.round.lifecycle_state = RoundLifecycleStates.EVALUATING
        self.round.save(update_fields=["lifecycle_state"])
        self.requested_offering = factories.RequestedOfferingFactory(call=self.call)
        offering = self.requested_offering.offering
        marketplace_factories.OfferingComponentFactory(offering=offering, type="cpu")
        marketplace_factories.OfferingComponentFactory(
            offering=offering, type="gpu", name="GPU"
        )
        self.first = self.make_proposal()
        self.second = self.make_proposal()

    def make_proposal(self, **kwargs):
        proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.IN_REVIEW, **kwargs
        )
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        factories.RequestedResourceFactory(
            proposal=proposal,
            requested_offering=self.requested_offering,
            limits={"cpu": 100},
        )
        return proposal

    def rank(self, proposal, rank, user=None, **extra):
        self.client.force_authenticate(user or self.call_manager)
        return self.client.post(
            RANKINGS_URL,
            {
                "round": self.round.uuid.hex,
                "proposal": proposal.uuid.hex,
                "rank": rank,
                **extra,
            },
            format="json",
        )

    def list_rankings(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(RANKINGS_URL, {"round_uuid": self.round.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data


class RankingWriteTest(RankingTestBase):
    def test_call_manager_ranks_proposals(self):
        offering_uuid = self.requested_offering.uuid.hex
        response = self.rank(
            self.first,
            1,
            final_score="4.5",
            recommended_resources={offering_uuid: {"cpu": 80, "gpu": 4}},
            notes="Strong case",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        ranking = models.ProposalRanking.objects.get(proposal=self.first)
        self.assertEqual(ranking.rank, 1)
        self.assertEqual(str(ranking.final_score), "4.500")
        self.assertEqual(
            ranking.recommended_resources, {offering_uuid: {"cpu": 80, "gpu": 4}}
        )
        self.assertEqual(ranking.created_by, self.call_manager)

    def test_recommendation_is_named_for_reading(self):
        offering = self.requested_offering.offering
        offering_uuid = self.requested_offering.uuid.hex
        response = self.rank(
            self.first,
            1,
            recommended_resources={offering_uuid: {"gpu": 4, "cpu": 80}},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        detail = response.data["recommended_resources_detail"]
        self.assertEqual(len(detail), 1)
        self.assertEqual(detail[0]["requested_offering_uuid"], offering_uuid)
        self.assertEqual(detail[0]["offering_name"], offering.name)
        names = {
            component["type"]: (component["name"], component["amount"])
            for component in detail[0]["components"]
        }
        cpu = offering.components.get(type="cpu")
        self.assertEqual(names, {"cpu": (cpu.name or "cpu", 80), "gpu": ("GPU", 4)})
        # In the offering's own component order.
        self.assertEqual(
            [component["type"] for component in detail[0]["components"]],
            [
                component.type
                for component in offering.components.all()
                if component.type in ("cpu", "gpu")
            ],
        )

    def test_rank_is_unique_within_the_round(self):
        self.assertEqual(self.rank(self.first, 1).status_code, status.HTTP_201_CREATED)
        response = self.rank(self.second, 1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("rank", response.data)

    def test_same_rank_in_another_round_is_fine(self):
        self.rank(self.first, 1)
        other_round = factories.RoundFactory(call=self.call)
        other = factories.ProposalFactory(
            round=other_round, state=ProposalStates.IN_REVIEW
        )
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            RANKINGS_URL,
            {"round": other_round.uuid.hex, "proposal": other.uuid.hex, "rank": 1},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_proposal_is_ranked_once_per_round(self):
        self.rank(self.first, 1)
        response = self.rank(self.first, 2)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_moving_a_rank_onto_a_taken_one_is_refused(self):
        self.rank(self.first, 1)
        self.rank(self.second, 2)
        ranking = models.ProposalRanking.objects.get(proposal=self.second)
        response = self.client.patch(ranking_url(ranking), {"rank": 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(ranking_url(ranking), {"rank": 3})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_proposal_of_another_round_is_refused(self):
        other = factories.ProposalFactory(state=ProposalStates.IN_REVIEW)
        response = self.rank(other, 1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_proposal_not_in_evaluation_is_refused(self):
        draft = factories.ProposalFactory(round=self.round, state=ProposalStates.DRAFT)
        response = self.rank(draft, 1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_recommendation_for_an_offering_not_requested_is_refused(self):
        other = factories.RequestedOfferingFactory(call=self.call)
        response = self.rank(
            self.first, 1, recommended_resources={other.uuid.hex: {"cpu": 1}}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_recommendation_for_an_unknown_component_is_refused(self):
        response = self.rank(
            self.first,
            1,
            recommended_resources={self.requested_offering.uuid.hex: {"ram": 1}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_negative_recommendation_is_refused(self):
        response = self.rank(
            self.first,
            1,
            recommended_resources={self.requested_offering.uuid.hex: {"cpu": -1}},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_panel_member_cannot_rank(self):
        response = self.rank(self.first, 1, user=self.panel_member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_ranking_is_fixed_once_the_round_goes_to_its_decision(self):
        self.rank(self.first, 1)
        ranking = models.ProposalRanking.objects.get(proposal=self.first)
        for state in (
            RoundLifecycleStates.DECIDING,
            RoundLifecycleStates.RESULTS_PUBLISHED,
            RoundLifecycleStates.CLOSED,
        ):
            self.round.lifecycle_state = state
            self.round.save(update_fields=["lifecycle_state"])
            response = self.client.patch(ranking_url(ranking), {"rank": 5})
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, state)
            response = self.rank(self.second, 2)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, state)
            response = self.client.delete(ranking_url(ranking))
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, state)
        ranking.refresh_from_db()
        self.assertEqual(ranking.rank, 1)

    def test_ranking_is_open_while_evaluating(self):
        self.rank(self.first, 1)
        ranking = models.ProposalRanking.objects.get(proposal=self.first)
        response = self.client.patch(ranking_url(ranking), {"notes": "Revisit"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_locked"] is False)


class RankingReadTest(RankingTestBase):
    def setUp(self):
        super().setUp()
        self.rank(self.first, 1)
        self.rank(self.second, 2)

    def test_call_manager_and_panel_member_read_the_ranking(self):
        for user in (self.call_manager, self.panel_member):
            data = self.list_rankings(user)
            self.assertEqual([row["rank"] for row in data], [1, 2])

    def test_applicant_and_reviewer_read_nothing(self):
        for user in (self.first.created_by, self.reviewer):
            self.assertEqual(self.list_rankings(user), [])
            ranking = models.ProposalRanking.objects.get(proposal=self.first)
            response = self.client.get(ranking_url(ranking))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def assert_reads_no_committee_data(self, user):
        self.assertEqual(self.list_rankings(user), [])
        response = self.client.get(
            RANKINGS_URL + "export_csv/", {"round_uuid": self.round.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.get(NOTES_URL, {"round_uuid": self.round.uuid.hex})
        self.assertEqual(response.data, [])
        document = models.RoundMeetingNoteDocument.objects.get()
        self.assertFalse(
            media_access.user_can_access_round_meeting_note_document(
                document.file, user
            )
        )

    def add_note(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            NOTES_URL,
            {
                "round": self.round.uuid.hex,
                "title": "Cut-off",
                "body": "Second: weak case.",
                "new_documents": [
                    SimpleUploadedFile("m.pdf", b"%PDF-1.4 x", "application/pdf")
                ],
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_panel_member_on_the_team_of_a_proposal_reads_nothing_of_the_round(self):
        self.add_note()
        third = self.make_proposal()
        self.rank(third, 3)
        self.second.add_user(self.panel_member, ProposalRole.MEMBER)
        # Not even the other proposals' rows: the gap would give theirs away.
        self.assert_reads_no_committee_data(self.panel_member)

    def test_panel_member_with_a_conflict_reads_nothing_of_the_round(self):
        self.add_note()
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.panel_member),
            proposal=self.second,
        )
        self.assert_reads_no_committee_data(self.panel_member)

    def test_a_dismissed_conflict_does_not_hide_the_round(self):
        factories.ConflictOfInterestFactory(
            reviewer=factories.ReviewerProfileFactory(user=self.panel_member),
            proposal=self.second,
            status=COIStatuses.DISMISSED,
        )
        self.assertEqual(len(self.list_rankings(self.panel_member)), 2)

    def test_staff_on_the_team_of_a_proposal_still_reads_the_round(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.second.add_user(staff, ProposalRole.MEMBER)
        self.assertEqual(len(self.list_rankings(staff)), 2)

    def test_conflicted_call_manager_cannot_rank_nor_learn_who_holds_a_rank(self):
        self.second.name = "Own proposal X"
        self.second.save(update_fields=["name"])
        self.second.add_user(self.call_manager, ProposalRole.MEMBER)
        fourth = self.make_proposal()
        response = self.rank(fourth, 2)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn("Own proposal X", str(response.data))
        own = self.make_proposal()
        own.add_user(self.call_manager, ProposalRole.MEMBER)
        response = self.rank(own, 9, final_score="9.9")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.ProposalRanking.objects.filter(proposal=own).exists())
        ranking = models.ProposalRanking.objects.get(proposal=self.first)
        response = self.client.patch(ranking_url(ranking), {"rank": 5}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.delete(ranking_url(ranking))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.post(
            NOTES_URL,
            {"round": self.round.uuid.hex, "title": "x"},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_nobody_ranks_a_proposal_they_are_on_the_team_of(self):
        staff = structure_factories.UserFactory(is_staff=True)
        own = self.make_proposal()
        own.add_user(staff, ProposalRole.MEMBER)
        response = self.rank(own, 3, user=staff)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.second.add_user(staff, ProposalRole.MEMBER)
        ranking = models.ProposalRanking.objects.get(proposal=self.second)
        response = self.client.patch(ranking_url(ranking), {"rank": 7}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.delete(ranking_url(ranking))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            models.ProposalRanking.objects.get(proposal=self.second).rank, 2
        )
        # Ranking the others is still fine for them.
        ranking = models.ProposalRanking.objects.get(proposal=self.first)
        response = self.client.patch(ranking_url(ranking), {"rank": 7}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_export_is_refused_to_applicant(self):
        self.client.force_authenticate(self.first.created_by)
        response = self.client.get(
            RANKINGS_URL + "export_csv/", {"round_uuid": self.round.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class RankingCsvTest(RankingTestBase):
    def export(self, user=None):
        self.client.force_authenticate(user or self.call_manager)
        response = self.client.get(
            RANKINGS_URL + "export_csv/", {"round_uuid": self.round.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return list(csv.DictReader(io.StringIO(response.content.decode())))

    def import_csv(self, rows, fieldnames=None):
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        self.client.force_authenticate(self.call_manager)
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

    def test_export_lists_unranked_proposals_for_managers(self):
        self.rank(
            self.first,
            1,
            recommended_resources={self.requested_offering.uuid.hex: {"cpu": 10}},
        )
        rows = self.export()
        self.assertEqual(len(rows), 2)
        column = f"recommended:{self.requested_offering.uuid.hex}:cpu"
        self.assertEqual(rows[0]["rank"], "1")
        self.assertEqual(rows[0][column], "10")
        self.assertEqual(rows[1]["rank"], "")
        self.assertEqual(len(self.export(self.panel_member)), 1)

    def test_exported_file_imports_back(self):
        rows = self.export()
        column = f"recommended:{self.requested_offering.uuid.hex}:gpu"
        for index, row in enumerate(rows, start=1):
            row["rank"] = str(index)
            row["final_score"] = "3.5"
            row[column] = "2"
        response = self.import_csv(rows)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data, {"created": 2, "updated": 0})
        ranking = models.ProposalRanking.objects.get(proposal=self.first)
        self.assertEqual(
            ranking.recommended_resources,
            {self.requested_offering.uuid.hex: {"gpu": 2}},
        )

    def test_import_can_swap_ranks(self):
        self.rank(self.first, 1)
        self.rank(self.second, 2)
        response = self.import_csv(
            [
                {"proposal_uuid": self.first.uuid.hex, "rank": "2"},
                {"proposal_uuid": self.second.uuid.hex, "rank": "1"},
            ]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            models.ProposalRanking.objects.get(proposal=self.first).rank, 2
        )

    def test_import_refuses_a_clash_and_saves_nothing(self):
        self.rank(self.first, 1)
        response = self.import_csv(
            [{"proposal_slug": self.second.slug, "rank": "1"}],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.ProposalRanking.objects.filter(proposal=self.second).exists()
        )

    def test_import_refuses_unknown_proposal(self):
        response = self.import_csv([{"proposal_uuid": "f" * 32, "rank": "1"}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def post_file(self, content):
        self.client.force_authenticate(self.call_manager)
        return self.client.post(
            RANKINGS_URL + "import_csv/",
            {
                "round": self.round.uuid.hex,
                "file": SimpleUploadedFile("ranking.csv", content, "text/csv"),
            },
            format="multipart",
        )

    def test_import_refuses_an_oversized_file(self):
        header = b"proposal_uuid,rank,notes\n"
        line = f"{self.first.uuid.hex},1,".encode()
        padding = b"x" * (rankings.MAX_IMPORT_BYTES - len(header) - len(line) + 1)
        response = self.post_file(header + line + padding + b"\n")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("larger than", str(response.data["file"]))
        self.assertFalse(models.ProposalRanking.objects.exists())

    def test_import_refuses_more_rows_than_the_round_can_have(self):
        too_many = (
            models.Proposal.objects.filter(round=self.round).count()
            + rankings.IMPORT_ROW_MARGIN
            + 1
        )
        # Rows without a rank are valid, and still read one by one.
        content = "proposal_uuid,rank\n" + "\n".join(
            f"{self.first.uuid.hex}," for _ in range(too_many)
        )
        response = self.post_file(content.encode())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("more rows", str(response.data["file"]))

    def test_import_refused_once_ranking_is_fixed(self):
        self.round.lifecycle_state = RoundLifecycleStates.DECIDING
        self.round.save(update_fields=["lifecycle_state"])
        response = self.import_csv(
            [{"proposal_uuid": self.first.uuid.hex, "rank": "1"}]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def import_values(self, **values):
        return self.import_csv(
            [{"proposal_uuid": self.first.uuid.hex, "rank": "1", **values}]
        )

    def assert_line_refused(self, response, *words):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = str(response.data["file"])
        self.assertIn("Line 2", message)
        for word in words:
            self.assertIn(word, message)
        self.assertFalse(models.ProposalRanking.objects.exists())

    def test_import_refuses_a_non_finite_recommended_amount(self):
        column = f"recommended:{self.requested_offering.uuid.hex}:cpu"
        for raw in ("nan", "inf", "-Infinity"):
            self.assert_line_refused(self.import_values(**{column: raw}), column)

    def test_import_refuses_a_final_score_out_of_range_or_not_a_number(self):
        for raw in ("1e20", "NaN", "Infinity", "12345678.5"):
            self.assert_line_refused(self.import_values(final_score=raw), "final_score")

    def test_import_refuses_a_rank_out_of_range(self):
        for raw in ("99999999999", "0", "abc"):
            self.assert_line_refused(self.import_values(rank=raw), "rank")

    def test_export_quotes_cells_a_spreadsheet_would_run(self):
        self.first.name = '=HYPERLINK("http://evil","x")'
        self.first.save(update_fields=["name"])
        self.rank(self.first, 1, notes="+1 strong; -2 weak")
        rows = self.export()
        self.assertEqual(rows[0]["proposal_name"], "'" + self.first.name)
        self.assertEqual(rows[0]["notes"], "'+1 strong; -2 weak")
        # The quote is dropped again when the file comes back.
        response = self.import_csv(rows)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            models.ProposalRanking.objects.get(proposal=self.first).notes,
            "+1 strong; -2 weak",
        )

    def test_import_refuses_a_row_for_a_proposal_on_the_importers_team(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.first.add_user(staff, ProposalRole.MEMBER)
        buffer = io.StringIO("proposal_uuid,rank\n" + self.first.uuid.hex + ",1\n")
        self.client.force_authenticate(staff)
        response = self.client.post(
            RANKINGS_URL + "import_csv/",
            {
                "round": self.round.uuid.hex,
                "file": SimpleUploadedFile("r.csv", buffer.getvalue().encode()),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(models.ProposalRanking.objects.exists())

    def test_panel_member_cannot_import(self):
        self.client.force_authenticate(self.panel_member)
        response = self.client.post(
            RANKINGS_URL + "import_csv/",
            {
                "round": self.round.uuid.hex,
                "file": SimpleUploadedFile("r.csv", b"proposal_uuid,rank\n"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class MeetingNoteTest(RankingTestBase):
    def create_note(self, user=None, **extra):
        self.client.force_authenticate(user or self.call_manager)
        return self.client.post(
            NOTES_URL,
            {"round": self.round.uuid.hex, "title": "Cut-off meeting", **extra},
            format="multipart",
        )

    def test_call_manager_records_a_note_with_documents(self):
        pdf = SimpleUploadedFile(
            "minutes.pdf", b"%PDF-1.4 minutes", content_type="application/pdf"
        )
        response = self.create_note(body="Ranked 12 proposals.", new_documents=[pdf])
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        note = models.RoundMeetingNote.objects.get()
        self.assertEqual(note.author, self.call_manager)
        self.assertEqual(note.documents.count(), 1)
        self.assertEqual(response.data["documents"][0]["name"], "minutes.pdf")

    def test_documents_can_be_removed(self):
        pdf = SimpleUploadedFile(
            "minutes.pdf", b"%PDF-1.4 minutes", content_type="application/pdf"
        )
        self.create_note(new_documents=[pdf])
        note = models.RoundMeetingNote.objects.get()
        document = note.documents.get()
        url = "http://testserver" + reverse(
            "round-meeting-note-detail", kwargs={"uuid": note.uuid.hex}
        )
        response = self.client.patch(
            url, {"removed_documents": [document.uuid.hex]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(note.documents.count(), 0)

    def test_panel_member_reads_but_does_not_write(self):
        self.create_note()
        self.client.force_authenticate(self.panel_member)
        response = self.client.get(NOTES_URL, {"round_uuid": self.round.uuid.hex})
        self.assertEqual(len(response.data), 1)
        response = self.create_note(user=self.panel_member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_applicant_and_reviewer_read_nothing(self):
        self.create_note()
        for user in (self.first.created_by, self.reviewer):
            self.client.force_authenticate(user)
            response = self.client.get(NOTES_URL)
            self.assertEqual(response.data, [])


class CompleteRoundStepTest(RankingTestBase):
    def setUp(self):
        super().setUp()
        self.third = self.make_proposal()
        for proposal in (self.first, self.second):
            self.place(proposal, "panel_review")
        self.place(self.third, "administrative_check")
        self.panel_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="panel_review"
        )

    def place(self, proposal, active_key):
        """Give the proposal every default step, ``active_key`` the active one."""
        reached = False
        active = None
        for step_def in DEFAULT_CALL_STEPS:
            factories.ensure_default_step(self.call, step_def.key)
            if step_def.key == active_key:
                step_status = WorkflowStepInstanceStatuses.ACTIVE
                reached = True
            elif step_def.key in ("expert_review", "technical_assessment"):
                step_status = WorkflowStepInstanceStatuses.SKIPPED
            elif not reached:
                step_status = WorkflowStepInstanceStatuses.COMPLETED
            else:
                step_status = WorkflowStepInstanceStatuses.PENDING
            instance = factories.ProposalWorkflowStepInstanceFactory(
                proposal=proposal,
                step=step_def.key,
                status=step_status,
                started_at=timezone.now(),
            )
            if step_status == WorkflowStepInstanceStatuses.ACTIVE:
                active = instance
        proposal.current_step = active
        proposal.save(update_fields=["current_step"])

    def complete(self, data=None, user=None):
        self.client.force_authenticate(user or self.call_manager)
        url = factories.RoundFactory.get_url(
            self.call, self.round, "complete_workflow_step"
        )
        payload = {"step": "panel_review", "outcome": "approved", **(data or {})}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, payload, format="json")

    def test_completes_the_step_for_every_proposal_at_it(self):
        with mock.patch(STEP_EVENT) as step_event:
            response = self.complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {row["proposal_uuid"] for row in response.data["completed"]},
            {self.first.uuid.hex, self.second.uuid.hex},
        )
        self.assertEqual(
            [row["proposal_uuid"] for row in response.data["skipped"]],
            [self.third.uuid.hex],
        )
        for proposal in (self.first, self.second):
            instance = proposal.workflow_step_instances.get(step="panel_review")
            self.assertEqual(instance.status, WorkflowStepInstanceStatuses.COMPLETED)
            self.assertEqual(instance.outcome, "approved")
            self.assertEqual(instance.completed_by, self.call_manager)
            proposal.refresh_from_db()
            self.assertEqual(proposal.current_step.step, "allocation_decision")
        third = self.third.workflow_step_instances.get(step="panel_review")
        self.assertEqual(third.status, WorkflowStepInstanceStatuses.PENDING)
        step_event.assert_not_called()  # no notification rules on this call

    def test_running_it_again_changes_nothing(self):
        self.complete()
        before = list(
            models.ProposalWorkflowStepInstance.objects.order_by("pk").values(
                "pk", "status", "outcome", "completed_at", "sent_notifications"
            )
        )
        response = self.complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["completed"], [])
        self.assertEqual(len(response.data["skipped"]), 3)
        after = list(
            models.ProposalWorkflowStepInstance.objects.order_by("pk").values(
                "pk", "status", "outcome", "completed_at", "sent_notifications"
            )
        )
        self.assertEqual(before, after)

    def test_notifications_fire_once_per_proposal(self):
        models.CallWorkflowStepNotificationRule.objects.create(
            workflow_step=self.panel_step,
            trigger=NotificationRuleTriggers.STEP_COMPLETED,
            recipient=NotificationRuleRecipients.CALL_MANAGERS,
        )
        with mock.patch(STEP_EVENT) as step_event:
            self.complete()
            self.complete()
        completed_instances = {
            proposal.workflow_step_instances.get(step="panel_review").uuid.hex
            for proposal in (self.first, self.second)
        }
        self.assertEqual(
            {call.args[0] for call in step_event.call_args_list},
            completed_instances,
        )
        self.assertEqual(step_event.call_count, 2)

    def test_step_can_be_named_by_uuid(self):
        self.client.force_authenticate(self.call_manager)
        url = factories.RoundFactory.get_url(
            self.call, self.round, "complete_workflow_step"
        )
        response = self.client.post(
            url,
            {"step_uuid": self.panel_step.uuid.hex, "outcome": "approved"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["completed"]), 2)

    def reason_seen_by(self, proposal, step, user):
        self.client.force_authenticate(user)
        url = factories.ProposalFactory.get_url(proposal, action="workflow_states")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = next(row for row in response.data if row["step"] == step)
        return row["outcome_reason"]

    def released_step(self):
        """The technical assessment, with its report released to the applicant,
        active for the first and second proposal."""
        for proposal in (self.first, self.second):
            proposal.workflow_step_instances.all().delete()
            self.place(proposal, "technical_assessment")
        call_step = models.CallWorkflowStep.objects.get(
            call=self.call, key="technical_assessment"
        )
        call_step.release_to_applicant = True
        call_step.save(update_fields=["release_to_applicant"])
        self.call.reviews_visible_to_submitters = True
        self.call.save(update_fields=["reviews_visible_to_submitters"])
        return call_step

    def test_feedback_on_a_released_step_waits_for_the_release(self):
        self.released_step()
        text = "Feasible on the GPU partition."
        response = self.complete(
            {
                "step": "technical_assessment",
                "outcome": "feasible",
                "feedback": [{"proposal_uuid": self.first.uuid.hex, "feedback": text}],
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data["completed"]), 2, response.data)
        instance = self.first.workflow_step_instances.get(step="technical_assessment")
        self.assertEqual(instance.outcome_reason, text)
        self.assertTrue(instance.feedback_withheld)
        step = "technical_assessment"
        self.assertEqual(
            self.reason_seen_by(self.first, step, self.first.created_by), ""
        )
        self.assertEqual(self.reason_seen_by(self.first, step, self.call_manager), text)
        instance.released_at = timezone.now()
        instance.save(update_fields=["released_at"])
        self.assertEqual(
            self.reason_seen_by(self.first, step, self.first.created_by), text
        )

    def test_feedback_on_a_released_step_completed_on_the_proposal_waits_too(self):
        self.released_step()
        text = "Feasible on the GPU partition."
        instance = self.first.workflow_step_instances.get(step="technical_assessment")
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            factories.ProposalFactory.get_url(
                self.first, action="complete_workflow_step"
            ),
            {
                "step_uuid": instance.uuid.hex,
                "outcome": "feasible",
                "outcome_reason": text,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        instance.refresh_from_db()
        self.assertTrue(instance.feedback_withheld)
        step = "technical_assessment"
        self.assertEqual(
            self.reason_seen_by(self.first, step, self.first.created_by), ""
        )
        instance.released_at = timezone.now()
        instance.save(update_fields=["released_at"])
        self.assertEqual(
            self.reason_seen_by(self.first, step, self.first.created_by), text
        )

    def test_feedback_on_a_step_never_released_is_not_withheld(self):
        response = self.complete(
            {
                "feedback": [
                    {"proposal_uuid": self.first.uuid.hex, "feedback": "Strong."}
                ]
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        instance = self.first.workflow_step_instances.get(step="panel_review")
        self.assertEqual(instance.outcome_reason, "Strong.")
        self.assertFalse(instance.feedback_withheld)

    def test_a_failing_proposal_is_reported_and_the_rest_complete(self):
        later = self.make_proposal()
        self.place(later, "panel_review")
        real_advance = workflow_service._advance_to_next
        failing = {self.second.pk: ValidationError("The offering is archived.")}

        def advance(proposal, from_instance, acting_user=None):
            if proposal.pk in failing:
                raise failing[proposal.pk]
            return real_advance(proposal, from_instance, acting_user=acting_user)

        with mock.patch.object(workflow_service, "_advance_to_next", advance):
            response = self.complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {row["proposal_uuid"] for row in response.data["completed"]},
            {self.first.uuid.hex, later.uuid.hex},
        )
        self.assertEqual(
            [(row["proposal_uuid"], row["detail"]) for row in response.data["failed"]],
            [(self.second.uuid.hex, "The offering is archived.")],
        )
        second = self.second.workflow_step_instances.get(step="panel_review")
        self.assertEqual(second.status, WorkflowStepInstanceStatuses.ACTIVE)
        self.assertIsNone(second.completed_at)

        # An unexpected error is reported too, without the internals.
        failing[self.second.pk] = RuntimeError("connection reset")
        with mock.patch.object(workflow_service, "_advance_to_next", advance):
            response = self.complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["completed"], [])
        self.assertEqual(len(response.data["skipped"]), 3)
        self.assertEqual(
            [row["proposal_uuid"] for row in response.data["failed"]],
            [self.second.uuid.hex],
        )
        self.assertNotIn("connection reset", response.data["failed"][0]["detail"])
        second.refresh_from_db()
        self.assertEqual(second.status, WorkflowStepInstanceStatuses.ACTIVE)

    def test_the_allocation_decision_is_refused(self):
        for proposal in (self.first, self.second):
            proposal.workflow_step_instances.all().delete()
            self.place(proposal, "allocation_decision")
        response = self.complete({"step": "allocation_decision"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("step", response.data)
        for proposal in (self.first, self.second):
            instance = proposal.workflow_step_instances.get(step="allocation_decision")
            self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)
            self.assertFalse(
                models.AwardedResource.objects.filter(proposal=proposal).exists()
            )

    def test_feedback_for_a_proposal_of_another_round_is_refused(self):
        other = factories.ProposalFactory()
        response = self.complete(
            {"feedback": [{"proposal_uuid": other.uuid.hex, "feedback": "x"}]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_outcome_outside_the_step_is_refused(self):
        response = self.complete({"outcome": "eligible"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_step_is_refused(self):
        response = self.complete({"step": "no_such_step"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_panel_member_and_applicant_cannot_complete_for_the_round(self):
        for user in (self.panel_member, self.first.created_by):
            response = self.complete(user=user)
            self.assertIn(
                response.status_code,
                (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
            )
        instance = self.first.workflow_step_instances.get(step="panel_review")
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)

    def test_a_gate_not_met_fails_that_proposal_alone(self):
        self.panel_step.min_contributions = 1
        self.panel_step.save(update_fields=["min_contributions"])
        response = self.complete()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["failed"]), 2)
        instance = self.first.workflow_step_instances.get(step="panel_review")
        self.assertEqual(instance.status, WorkflowStepInstanceStatuses.ACTIVE)
