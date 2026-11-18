"""A call can require a minimum number of supporting documents (#1222)."""

from unittest import mock

import yaml
from ddt import data, ddt
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_mastermind.proposal import models, views
from waldur_mastermind.proposal.enums import ProposalFieldStates, ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures
from waldur_mastermind.proposal.tests.test_change_requests import ChangeRequestBase
from waldur_mastermind.proposal.tests.test_proposal_admin import ProposalAdminMixin


def attach_documents(proposal, count):
    for index in range(count):
        models.ProposalDocumentation.objects.create(
            proposal=proposal,
            file=SimpleUploadedFile(f"annex-{index}.pdf", b"%PDF-1.4\n%%EOF\n"),
        )


def configure(call, state=ProposalFieldStates.REQUIRED, minimum=1):
    """Write the config row directly: the API refuses to tighten a call that
    already has proposals, which is covered by ConfigurationTest."""
    models.CallProposalFieldConfig.objects.filter(call=call).update(
        field_supporting_documentation=state,
        minimum_supporting_documents=minimum,
    )
    # The reverse one-to-one cache would still hold the old row.
    call.refresh_from_db()


@ddt
@mock.patch(
    "waldur_mastermind.proposal.views.tasks.notify_user_about_proposal_state_update.delay"
)
class SubmitTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.call = self.proposal.round.call
        self.proposal.add_user(self.proposal.created_by, ProposalRole.MANAGER)
        # The no-workflow submission path: DRAFT -> SUBMITTED.
        models.CallWorkflowStep.objects.filter(call=self.call).delete()
        self.url = factories.ProposalFactory.get_url(self.proposal, "submit")
        self.client.force_authenticate(self.fixture.staff)

    def submit(self):
        return self.client.post(self.url)

    def can_submit(self):
        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["can_submit"]

    def test_below_the_minimum_is_refused_naming_both_numbers(self, _notify):
        configure(self.call, minimum=3)
        attach_documents(self.proposal, 2)

        verdict = self.can_submit()
        response = self.submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = "This call requires at least 3 supporting documents; 2 attached."
        self.assertIn(message, response.data)
        self.assertEqual(verdict, {"can_submit": False, "error": message})
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)

    def test_reaching_the_minimum_submits(self, _notify):
        configure(self.call, minimum=3)
        attach_documents(self.proposal, 3)

        self.assertEqual(self.can_submit(), {"can_submit": True, "error": None})
        response = self.submit()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.SUBMITTED)

    def test_default_minimum_refuses_none(self, _notify):
        configure(self.call)

        verdict = self.can_submit()
        response = self.submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = "This call requires at least 1 supporting document; 0 attached."
        self.assertIn(message, response.data)
        self.assertEqual(verdict["error"], message)

    def test_default_minimum_accepts_one(self, _notify):
        configure(self.call)
        attach_documents(self.proposal, 1)

        response = self.submit()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data(ProposalFieldStates.OPTIONAL, ProposalFieldStates.HIDDEN)
    def test_minimum_is_ignored_unless_required(self, state, _notify):
        configure(self.call, state=state, minimum=5)

        self.assertEqual(self.can_submit(), {"can_submit": True, "error": None})
        response = self.submit()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_other_empty_fields_are_reported_with_the_shortfall(self, _notify):
        configure(self.call, minimum=2)
        self.proposal.project_summary = ""
        self.proposal.save()

        response = self.submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_summary", str(response.data))
        self.assertIn("at least 2 supporting documents; 0 attached", str(response.data))

    def test_a_document_detached_before_the_lock_is_counted_under_it(self, _notify):
        # The validators counted two; a detach that commits before submit
        # takes the row lock leaves one. The first check taken under the lock
        # stands in for that moment.
        configure(self.call, minimum=2)
        attach_documents(self.proposal, 2)
        validate_call_is_active = views.validate_call_is_active

        def detach_one_then_validate(proposal):
            proposal.proposaldocumentation_set.first().delete()
            validate_call_is_active(proposal)

        with mock.patch.object(
            views, "validate_call_is_active", side_effect=detach_one_then_validate
        ):
            response = self.submit()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "This call requires at least 2 supporting documents; 1 attached.",
            response.data,
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.state, ProposalStates.DRAFT)
        self.assertIsNone(self.proposal.submitted_at)

    def test_model_reads_the_call_without_the_annotations(self, _notify):
        configure(self.call, minimum=2)
        attach_documents(self.proposal, 1)
        proposal = models.Proposal.objects.get(pk=self.proposal.pk)

        self.assertEqual(proposal.supporting_documents_shortfall(), (2, 1))
        self.assertEqual(
            proposal.can_submit(),
            (False, "This call requires at least 2 supporting documents; 1 attached."),
        )


class ResubmitChangesTest(ChangeRequestBase):
    """Submitting changes enforces the minimum only when documents were
    unlocked: otherwise the applicant could not do anything about it."""

    def setUp(self):
        super().setUp()
        configure(self.call, minimum=2)

    def test_unlocked_documents_must_reach_the_minimum(self):
        self.request_changes(parts=["documents"])

        response = self.submit_changes()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "This call requires at least 2 supporting documents; 0 attached.",
            response.data,
        )

        attach_documents(self.proposal, 2)
        response = self.submit_changes()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_can_submit_reports_the_shortfall_while_documents_are_unlocked(self):
        self.request_changes(parts=["documents"])
        self.client.force_authenticate(self.applicant)

        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))

        self.assertEqual(
            response.data["can_submit"],
            {
                "can_submit": False,
                "error": "This call requires at least 2 supporting documents; "
                "0 attached.",
            },
        )

    def test_changes_that_did_not_unlock_documents_ignore_the_minimum(self):
        self.request_changes(parts=["field:project_summary"])
        self.update_details(project_summary="Names the codes it benchmarks")

        response = self.submit_changes()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class DetachTest(ProposalAdminMixin, test.APITestCase):
    """Detaching locks the proposal and asks again whether the applicant may
    still edit it, so it cannot slip in behind a submission."""

    def setUp(self):
        super().setUp()
        configure(self.proposal.round.call, minimum=2)
        attach_documents(self.proposal, 2)
        self.document = self.proposal.proposaldocumentation_set.first()

    def detach(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            self._url("detach_documents"), {"documents": [self.document.uuid.hex]}
        )

    def test_applicant_detaches_from_a_draft(self):
        response = self.detach(self.applicant)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.proposaldocumentation_set.count(), 1)

    def test_detach_after_the_proposal_was_submitted_is_refused(self):
        # The permission check passes on the draft; the submission commits
        # before the detach takes the row lock.
        proposal = self.proposal

        def passes_then_submitted(request, view, obj=None):
            models.Proposal.objects.filter(pk=proposal.pk).update(
                state=ProposalStates.SUBMITTED
            )

        with mock.patch.object(
            views.ProposalViewSet,
            "detach_documents_permissions",
            [passes_then_submitted],
        ):
            response = self.detach(self.applicant)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.proposal.proposaldocumentation_set.count(), 2)

    def test_call_overseer_still_detaches_from_a_submitted_proposal(self):
        self.proposal.state = ProposalStates.SUBMITTED
        self.proposal.save(update_fields=["state"])

        response = self.detach(self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.proposal.proposaldocumentation_set.count(), 1)


class ListQueriesTest(test.APITestCase):
    """The proposal list reads the minimum and the document count from
    ``can_submit_annotations``, as ``ProposalViewSet.get_queryset`` applies
    them. Measured on the queryset rather than the endpoint: the list
    serializer has per-row reads of its own, which are not can_submit's."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.round = self.fixture.round
        configure(self.fixture.call, minimum=3)

    def add_proposals(self, count):
        for index in range(count):
            proposal = factories.ProposalFactory(round=self.round)
            attach_documents(proposal, index % 4)

    def verdicts(self):
        queryset = (
            models.Proposal.objects.filter(round=self.round)
            .prefetch_related(
                "requestedresource_set__requested_offering__offering__components"
            )
            .select_related("round__call")
            .annotate(**models.can_submit_annotations())
        )
        with CaptureQueriesContext(connection) as captured:
            verdicts = {proposal.pk: proposal.can_submit() for proposal in queryset}
        return verdicts, len(captured.captured_queries)

    def test_listing_adds_no_query_per_proposal(self):
        self.add_proposals(2)
        _, few = self.verdicts()

        self.add_proposals(5)
        verdicts, many = self.verdicts()

        self.assertEqual(
            few, many, f"{few} queries for 2 proposals, {many} for 7: one per row."
        )
        # And the annotations agree with what the model reads on its own.
        for proposal in models.Proposal.objects.filter(round=self.round):
            self.assertEqual(verdicts[proposal.pk], proposal.can_submit())
        errors = {error for _, error in verdicts.values()}
        self.assertIn(
            "This call requires at least 3 supporting documents; 1 attached.", errors
        )

    def test_a_call_without_a_config_row_requires_nothing(self):
        models.CallProposalFieldConfig.objects.filter(call=self.fixture.call).delete()
        proposal = factories.ProposalFactory(round=self.round)

        annotated = models.Proposal.objects.annotate(
            **models.can_submit_annotations()
        ).get(pk=proposal.pk)

        self.assertEqual(annotated._minimum_supporting_documents, 0)
        self.assertEqual(annotated._supporting_documents_count, 0)
        self.assertIsNone(annotated.supporting_documents_shortfall())


@ddt
class ConfigurationTest(test.APITestCase):
    """The fixture's call already has proposals; ``new_call`` has none."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.call.add_user(self.fixture.call_manager, CallRole.MANAGER)
        configure(self.call, minimum=2)

    def patch(self, minimum, user="call_manager", call=None):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(
            factories.CallFactory.get_protected_url(call or self.call),
            {"proposal_field_config": {"minimum_supporting_documents": minimum}},
            format="json",
        )

    def stored(self, call=None):
        return models.CallProposalFieldConfig.objects.get(
            call=call or self.call
        ).minimum_supporting_documents

    def test_new_call_defaults_to_one(self):
        call = factories.CallFactory()

        self.assertEqual(self.stored(call), 1)

    def test_minimum_is_read_with_the_field_config(self):
        self.client.force_authenticate(self.fixture.call_manager)

        response = self.client.get(factories.CallFactory.get_protected_url(self.call))

        self.assertEqual(
            response.data["proposal_field_config"]["minimum_supporting_documents"], 2
        )

    @data(0, -1)
    def test_minimum_below_one_is_refused(self, minimum):
        response = self.patch(minimum, call=self.fixture.new_call, user="staff")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("minimum_supporting_documents", str(response.data))
        self.assertEqual(self.stored(self.fixture.new_call), 1)

    def test_raising_is_refused_once_the_call_has_proposals(self):
        response = self.patch(3)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cannot raise the minimum", str(response.data))
        self.assertIn("Duplicate the call", str(response.data))
        self.assertEqual(self.stored(), 2)

    @data(1, 2)
    def test_lowering_or_restating_is_accepted_with_proposals(self, minimum):
        response = self.patch(minimum)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(), minimum)

    @data(1, 5)
    def test_call_without_proposals_accepts_any_change(self, minimum):
        empty_call = self.fixture.new_call
        configure(empty_call, minimum=2)

        response = self.patch(minimum, user="staff", call=empty_call)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.stored(empty_call), minimum)

    @data("user", "owner", "proposal_creator", "reviewer_1")
    def test_only_those_who_update_the_call_change_it(self, user):
        response = self.patch(1, user=user)

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertEqual(self.stored(), 2)


class CopyTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        configure(self.call, minimum=4)
        self.client.force_authenticate(self.fixture.staff)

    def test_duplicate_keeps_the_minimum(self):
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call, "duplicate"),
            {"name": "Copy of the call"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        config = models.CallProposalFieldConfig.objects.get(
            call__uuid=response.data["uuid"]
        )
        self.assertEqual(config.minimum_supporting_documents, 4)
        self.assertEqual(
            config.field_supporting_documentation, ProposalFieldStates.REQUIRED
        )

    def test_export_and_import_keep_the_minimum(self):
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call, "export_call"),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        exported = response.data["export_data"]
        self.assertEqual(
            exported["proposal_field_config"]["minimum_supporting_documents"], 4
        )

        response = self.client.post(
            factories.CallFactory.get_protected_list_url("import_call"),
            {
                "manager": self.fixture.manager.uuid.hex,
                "call_data": yaml.safe_dump(exported),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        config = models.CallProposalFieldConfig.objects.get(
            call__uuid=response.data["call_uuid"]
        )
        self.assertEqual(config.minimum_supporting_documents, 4)
