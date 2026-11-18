import csv
import io
import uuid
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import assignments, models, rankings
from waldur_mastermind.proposal.enums import (
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    AssignmentSources,
    COIStatuses,
    ProposalStates,
    ReviewerPoolInvitationStatuses,
)

from . import factories


class AssignmentImportTest(test.APITestCase):
    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        self.call_manager = structure_factories.UserFactory()
        self.call.manager.add_user(self.call_manager, CallRole.MANAGER)
        self.round = factories.RoundFactory(call=self.call)
        self.alice = self.make_reviewer("alice@example.com")
        self.bob = self.make_reviewer("bob@example.com")
        self.first = self.make_proposal()
        self.second = self.make_proposal()

    def make_reviewer(self, email, **kwargs):
        kwargs.setdefault("invitation_status", ReviewerPoolInvitationStatuses.ACCEPTED)
        profile = factories.ReviewerProfileFactory(
            user=structure_factories.UserFactory(email=email)
        )
        return factories.CallReviewerPoolFactory(
            call=self.call, reviewer=profile, **kwargs
        )

    def make_proposal(self, **kwargs):
        kwargs.setdefault("state", ProposalStates.SUBMITTED)
        kwargs.setdefault("round", self.round)
        return factories.ProposalFactory(**kwargs)

    def url(self, action="import-assignments"):
        return factories.CallFactory.get_protected_url(self.call, action=action)

    def post_file(self, content, user=None, **data):
        self.client.force_authenticate(user or self.call_manager)
        payload = {
            "round": self.round.uuid.hex,
            "file": SimpleUploadedFile("assignments.csv", content, "text/csv"),
        }
        payload.update(data)
        return self.client.post(self.url(), payload, format="multipart")

    def post_rows(self, rows, user=None, fieldnames=None, **data):
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer, fieldnames=fieldnames or ["proposal_slug", "reviewer_email"]
        )
        writer.writeheader()
        writer.writerows(rows)
        return self.post_file(buffer.getvalue().encode(), user=user, **data)

    def apply(self, rows, **data):
        return self.post_rows(rows, **{**data, "dry_run": "false"})

    def row(self, proposal, pool_entry):
        return {
            "proposal_slug": proposal.slug,
            "reviewer_email": pool_entry.reviewer.user.email,
        }

    def statuses(self, response):
        return [(row["row"], row["status"]) for row in response.data["rows"]]

    def test_dry_run_is_the_default_and_creates_nothing(self):
        response = self.post_rows(
            [self.row(self.first, self.alice), self.row(self.second, self.bob)]
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["dry_run"])
        self.assertEqual(self.statuses(response), [(2, "ready"), (3, "ready")])
        self.assertEqual(response.data["totals"]["ready"], 2)
        self.assertEqual(response.data["batch_uuids"], [])
        first = response.data["rows"][0]
        self.assertEqual(first["proposal_slug"], self.first.slug)
        self.assertEqual(first["proposal_uuid"], str(self.first.uuid))
        self.assertEqual(first["reviewer_email"], "alice@example.com")
        self.assertFalse(models.AssignmentBatch.objects.exists())
        self.assertFalse(models.AssignmentItem.objects.exists())

    def test_apply_creates_one_draft_manual_batch_per_reviewer(self):
        response = self.apply(
            [
                self.row(self.first, self.alice),
                self.row(self.second, self.alice),
                self.row(self.first, self.bob),
            ],
            manager_notes="Panel decision",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["dry_run"])
        self.assertEqual(response.data["totals"]["created"], 3)
        batches = models.AssignmentBatch.objects.filter(call=self.call)
        self.assertEqual(batches.count(), 2)
        self.assertCountEqual(
            response.data["batch_uuids"], [str(batch.uuid) for batch in batches]
        )
        alice_batch = batches.get(reviewer_pool_entry=self.alice)
        self.assertEqual(alice_batch.status, AssignmentBatchStatuses.DRAFT)
        self.assertEqual(alice_batch.source, AssignmentSources.MANUAL)
        self.assertEqual(alice_batch.manager_notes, "Panel decision")
        self.assertEqual(alice_batch.created_by, self.call_manager)
        self.assertCountEqual(
            alice_batch.items.values_list("proposal", flat=True),
            [self.first.pk, self.second.pk],
        )

    def test_import_matches_manual_assignment(self):
        self.client.force_authenticate(self.call_manager)
        manual = self.client.post(
            self.url("create-manual-assignment"),
            {
                "reviewer_pool_entry_uuid": self.alice.uuid.hex,
                "proposal_uuids": [self.first.uuid.hex, self.second.uuid.hex],
                "manager_notes": "Same",
            },
            format="json",
        )
        imported = self.apply(
            [self.row(self.first, self.bob), self.row(self.second, self.bob)],
            manager_notes="Same",
        )
        self.assertEqual(manual.status_code, status.HTTP_200_OK, manual.data)
        self.assertEqual(imported.status_code, status.HTTP_200_OK, imported.data)

        def shape(pool_entry):
            batch = models.AssignmentBatch.objects.get(reviewer_pool_entry=pool_entry)
            batch_fields = (
                batch.status,
                batch.source,
                batch.manager_notes,
                batch.created_by_id,
            )
            items = sorted(
                batch.items.values_list(
                    "proposal_id", "status", "has_coi", "affinity_score"
                )
            )
            return batch_fields, items

        self.assertEqual(shape(self.alice), shape(self.bob))

    def test_extends_the_reviewers_existing_draft_batch(self):
        draft = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.alice,
            status=AssignmentBatchStatuses.DRAFT,
        )

        response = self.apply([self.row(self.first, self.alice)])

        self.assertEqual(response.data["batch_uuids"], [str(draft.uuid)])
        self.assertEqual(draft.items.get().proposal, self.first)

    def test_conflict_of_interest_gives_a_blocked_item(self):
        for coi_status in (COIStatuses.PENDING, COIStatuses.RECUSED):
            with self.subTest(coi_status=coi_status):
                proposal = self.make_proposal()
                factories.ConflictOfInterestFactory(
                    reviewer=self.alice.reviewer, proposal=proposal, status=coi_status
                )

                dry = self.post_rows([self.row(proposal, self.alice)])
                applied = self.apply([self.row(proposal, self.alice)])

                self.assertEqual(self.statuses(dry), [(2, "coi_blocked")])
                self.assertEqual(self.statuses(applied), [(2, "coi_blocked")])
                item = models.AssignmentItem.objects.get(proposal=proposal)
                self.assertEqual(item.status, AssignmentItemStatuses.COI_BLOCKED)
                self.assertTrue(item.has_coi)
                self.assertEqual(item.coi_records.count(), 1)

    def test_invalid_rows_are_reported_and_not_applied(self):
        sent = factories.AssignmentBatchFactory(
            call=self.call,
            reviewer_pool_entry=self.alice,
            status=AssignmentBatchStatuses.SENT,
        )
        factories.AssignmentItemFactory(batch=sent, proposal=self.first)
        draft = self.make_proposal(state=ProposalStates.DRAFT)
        other_round = self.make_proposal(round=factories.RoundFactory(call=self.call))
        pending = self.make_reviewer(
            "pending@example.com",
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

        response = self.apply(
            [
                self.row(self.first, self.alice),  # already assigned
                self.row(self.second, self.alice),  # created
                self.row(self.second, self.alice),  # repeated
                self.row(draft, self.alice),  # not under evaluation
                self.row(other_round, self.alice),  # not in the round
                self.row(self.second, pending),  # pool invitation not accepted
                {"proposal_slug": self.second.slug, "reviewer_email": "x@y.org"},
                {"proposal_slug": "no-such-id", "reviewer_email": "bob@example.com"},
                {"proposal_slug": self.second.slug, "reviewer_email": ""},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.statuses(response),
            [
                (2, "skipped"),
                (3, "created"),
                (4, "skipped"),
                (5, "error"),
                (6, "error"),
                (7, "error"),
                (8, "error"),
                (9, "error"),
                (10, "error"),
            ],
        )
        reasons = [row["reason"] for row in response.data["rows"]]
        self.assertIn("already exists", reasons[0])
        self.assertIn("line 3", reasons[2])
        self.assertIn("not under evaluation", reasons[3])
        self.assertIn("not in round", reasons[4])
        self.assertIn("No accepted reviewer", reasons[5])
        self.assertIn("No accepted reviewer", reasons[6])
        self.assertIn("No proposal", reasons[7])
        self.assertEqual(
            response.data["totals"],
            {"ready": 0, "created": 1, "coi_blocked": 0, "skipped": 2, "error": 6},
        )
        self.assertEqual(
            list(
                models.AssignmentItem.objects.filter(
                    batch__status=AssignmentBatchStatuses.DRAFT
                ).values_list("batch__reviewer_pool_entry", "proposal")
            ),
            [(self.alice.pk, self.second.pk)],
        )

    def test_proposal_uuid_of_another_call_is_not_found(self):
        elsewhere = factories.ProposalFactory(state=ProposalStates.SUBMITTED)

        response = self.post_rows(
            [
                {
                    "proposal_uuid": elsewhere.uuid.hex,
                    "reviewer_email": "alice@example.com",
                }
            ],
            fieldnames=["proposal_uuid", "reviewer_email"],
            dry_run="false",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.statuses(response), [(2, "error")])
        row = response.data["rows"][0]
        self.assertIn("No proposal of this call", row["reason"])
        self.assertIsNone(row["proposal_uuid"])
        self.assertEqual(row["proposal_name"], "")
        self.assertEqual(response.data["batch_uuids"], [])
        self.assertFalse(models.AssignmentBatch.objects.exists())
        self.assertFalse(models.AssignmentItem.objects.exists())

    def test_ambiguous_slug_asks_for_the_uuid(self):
        models.Proposal.objects.filter(pk=self.second.pk).update(slug=self.first.slug)

        by_slug = self.post_rows([self.row(self.first, self.alice)])
        by_uuid = self.post_rows(
            [
                {
                    "proposal_uuid": str(uuid.UUID(self.second.uuid.hex)),
                    "reviewer_email": "ALICE@example.com",
                }
            ],
            fieldnames=["proposal_uuid", "reviewer_email"],
        )

        self.assertEqual(self.statuses(by_slug), [(2, "error")])
        self.assertIn("proposal_uuid", by_slug.data["rows"][0]["reason"])
        # A UUID, with dashes or without, and the email in any case, resolve.
        self.assertEqual(self.statuses(by_uuid), [(2, "ready")])
        self.assertEqual(
            by_uuid.data["rows"][0]["proposal_uuid"], str(self.second.uuid)
        )

    def test_workload_limit_refuses_the_rows_above_it(self):
        self.alice.max_assignments = 1
        self.alice.save()
        third = self.make_proposal()
        rows = [
            self.row(self.first, self.alice),
            self.row(self.second, self.alice),
            self.row(third, self.alice),
        ]

        dry = self.post_rows(rows)
        applied = self.apply(rows)

        expected = [(2, "created"), (3, "error"), (4, "error")]
        self.assertEqual(self.statuses(dry), [(2, "ready"), (3, "error"), (4, "error")])
        self.assertEqual(self.statuses(applied), expected)
        self.assertIn("workload limit", applied.data["rows"][1]["reason"])
        self.assertEqual(
            models.AssignmentItem.objects.filter(
                batch__reviewer_pool_entry=self.alice
            ).count(),
            1,
        )
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.REVIEWER_WORKLOAD_LIMIT_OVERRIDDEN
            ).exists()
        )

    def test_override_assigns_above_the_limit_and_is_logged(self):
        self.alice.max_assignments = 1
        self.alice.save()

        response = self.apply(
            [self.row(self.first, self.alice), self.row(self.second, self.alice)],
            override_workload_limit="true",
        )

        self.assertEqual(self.statuses(response), [(2, "created"), (3, "created")])
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.REVIEWER_WORKLOAD_LIMIT_OVERRIDDEN
            ).exists()
        )

    @mock.patch(
        "waldur_mastermind.proposal.tasks.send_assignment_batch_invitation.delay"
    )
    def test_nothing_is_sent_until_a_manager_sends_the_batches(self, send):
        response = self.apply([self.row(self.first, self.alice)])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        batch = models.AssignmentBatch.objects.get(uuid=response.data["batch_uuids"][0])
        self.assertEqual(batch.status, AssignmentBatchStatuses.DRAFT)
        self.assertIsNone(batch.sent_at)
        send.assert_not_called()
        self.assertEqual(mail.outbox, [])

        self.client.post(
            self.url("send-all-assignments"),
            {"batch_uuids": response.data["batch_uuids"]},
            format="json",
        )
        batch.refresh_from_db()
        self.assertEqual(batch.status, AssignmentBatchStatuses.SENT)
        send.assert_called_once()

    def test_file_with_a_bom_is_read(self):
        content = f"reviewer_email,proposal_slug\nalice@example.com,{self.first.slug}\n"
        response = self.post_file(content.encode("utf-8-sig"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.statuses(response), [(2, "ready")])

    def test_refuses_an_oversized_file(self):
        with mock.patch.object(rankings, "MAX_IMPORT_BYTES", 40):
            response = self.post_file(
                b"proposal_slug,reviewer_email\n" + b"x," * 20 + b"\n"
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("larger than", str(response.data["file"]))

    def test_refuses_a_file_that_is_not_utf8(self):
        response = self.post_file(
            "proposal_slug,reviewer_email\nä,ö\n".encode("utf-16")
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("UTF-8", str(response.data["file"]))

    def test_refuses_a_field_longer_than_the_csv_limit(self):
        # Well within the size limit, but one field is over the CSV reader's.
        field = "x" * (csv.field_size_limit() + 1)
        content = f"proposal_slug,reviewer_email\n{field},alice@example.com\n"
        self.assertLess(len(content), rankings.MAX_IMPORT_BYTES)

        response = self.post_file(content.encode())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = str(response.data["file"])
        self.assertIn("Line 2", message)
        self.assertIn("longer than", message)
        self.assertNotIn("xxxx", message)

    def test_refuses_a_file_that_is_not_valid_csv(self):
        # A carriage return outside quotes inside a row, and in the header.
        for content in (
            f"proposal_slug,reviewer_email\n{self.first.slug}\rx,a@b.org\n",
            "proposal_slug\rx,reviewer_email\n",
        ):
            with self.subTest(content=content):
                response = self.post_file(content.encode())
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("not valid CSV", str(response.data["file"]))
        self.assertFalse(models.AssignmentBatch.objects.exists())

    def test_refuses_a_file_without_the_required_columns(self):
        for header in (b"proposal_slug,email\n", b"reviewer_email,name\n", b""):
            with self.subTest(header=header):
                response = self.post_file(header + b"a,b\n")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("reviewer_email", str(response.data["file"]))

    def test_refuses_more_rows_than_the_round_can_have(self):
        # Two proposals times two reviewers, and the margin for blank lines.
        too_many = 2 * 2 + rankings.IMPORT_ROW_MARGIN + 1
        content = "proposal_slug,reviewer_email\n" + "\n".join(
            f"{self.first.slug},alice@example.com" for _ in range(too_many)
        )
        response = self.post_file(content.encode())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("more rows", str(response.data["file"]))

    def test_refuses_a_round_of_another_call(self):
        other_round = factories.RoundFactory()
        response = self.post_rows(
            [self.row(self.first, self.alice)], round=other_round.uuid.hex
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("round", response.data)

    def test_only_those_who_create_manual_assignments_may_import(self):
        reviewer = self.alice.reviewer.user
        self.call.add_user(reviewer, CallRole.REVIEWER)
        for user, expected in (
            (structure_factories.UserFactory(is_staff=True), status.HTTP_200_OK),
            (self.call_manager, status.HTTP_200_OK),
            (reviewer, status.HTTP_403_FORBIDDEN),
            (structure_factories.UserFactory(), status.HTTP_404_NOT_FOUND),
        ):
            with self.subTest(user=user):
                response = self.post_rows([self.row(self.first, self.alice)], user)
                self.assertEqual(response.status_code, expected)
        # Only the two allowed users' dry runs ran: nothing was created.
        self.assertFalse(models.AssignmentBatch.objects.exists())

        self.client.force_authenticate(None)
        response = self.client.post(self.url(), {}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_call_manager_without_the_permission_may_not_import(self):
        CallRole.MANAGER.delete_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        response = self.post_rows([self.row(self.first, self.alice)])
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertFalse(models.AssignmentBatch.objects.exists())


class FindNewItemsTest(test.APITestCase):
    """``find_new_items`` is shared by ``create-manual-assignment`` and the
    import, and runs while the reviewer's pool entry is locked."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.entry = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )

    def proposal(self):
        return factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )

    def item(self, proposal, batch_status, item_status):
        batch = factories.AssignmentBatchFactory(
            call=self.call, reviewer_pool_entry=self.entry, status=batch_status
        )
        factories.AssignmentItemFactory(
            batch=batch, proposal=proposal, status=item_status
        )

    def coi(self, proposal, coi_status):
        return factories.ConflictOfInterestFactory(
            reviewer=self.entry.reviewer, proposal=proposal, status=coi_status
        )

    def test_splits_the_proposals_in_two_queries(self):
        free = self.proposal()
        live = self.proposal()  # a pending item in a sent batch
        self.item(live, AssignmentBatchStatuses.SENT, AssignmentItemStatuses.PENDING)
        declined = self.proposal()  # a declined item of a sent batch: free again
        self.item(
            declined, AssignmentBatchStatuses.SENT, AssignmentItemStatuses.DECLINED
        )
        in_draft = self.proposal()  # any item of the draft batch counts
        self.item(
            in_draft, AssignmentBatchStatuses.DRAFT, AssignmentItemStatuses.DECLINED
        )
        pending_coi = self.proposal()
        blocking = self.coi(pending_coi, COIStatuses.PENDING)
        dismissed_coi = self.proposal()
        self.coi(dismissed_coi, COIStatuses.DISMISSED)
        assigned_with_coi = self.proposal()
        self.item(
            assigned_with_coi,
            AssignmentBatchStatuses.SENT,
            AssignmentItemStatuses.ACCEPTED,
        )
        self.coi(assigned_with_coi, COIStatuses.RECUSED)
        # Another reviewer's assignment and conflict do not count.
        other = factories.CallReviewerPoolFactory(call=self.call)
        batch = factories.AssignmentBatchFactory(
            call=self.call, reviewer_pool_entry=other
        )
        factories.AssignmentItemFactory(batch=batch, proposal=free)
        factories.ConflictOfInterestFactory(
            reviewer=other.reviewer, proposal=free, status=COIStatuses.PENDING
        )
        proposals = [
            free,
            live,
            declined,
            in_draft,
            pending_coi,
            dismissed_coi,
            assigned_with_coi,
        ]

        with self.assertNumQueries(2):
            new_items, already_assigned = assignments.find_new_items(
                self.entry, proposals
            )

        self.assertEqual(
            new_items,
            [
                (free, []),
                (declined, []),
                (pending_coi, [blocking]),
                (dismissed_coi, []),
            ],
        )
        self.assertEqual(already_assigned, [live, in_draft, assigned_with_coi])

    def test_nothing_to_check_skips_the_conflict_query(self):
        assigned = self.proposal()
        self.item(
            assigned, AssignmentBatchStatuses.SENT, AssignmentItemStatuses.PENDING
        )

        with self.assertNumQueries(1):
            new_items, already_assigned = assignments.find_new_items(
                self.entry, [assigned]
            )

        self.assertEqual((new_items, already_assigned), ([], [assigned]))
