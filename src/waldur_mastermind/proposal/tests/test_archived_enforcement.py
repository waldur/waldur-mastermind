"""Backend read-only enforcement for archived calls.

An archived call must reject every mutation across its edit surface: the call
itself, nested offerings / resource templates / workflow steps, role mappings,
rounds and documents. Draft and active calls stay fully editable. These tests
lock in that contract and the ``has_proposals`` gate the frontend relies on.
"""

from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_core.permissions.fixtures import ProjectRole, ProposalRole
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import CallStates, ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


class ArchivedCallEnforcementTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.manager = self.fixture.call_manager
        self.client.force_authenticate(self.manager)

    def archive(self):
        self.call.state = CallStates.ARCHIVED
        self.call.save()

    # --- core call update -------------------------------------------------

    def test_update_call_allowed_when_active(self):
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(url, {"description": "new description"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.call.refresh_from_db()
        self.assertEqual(self.call.description, "new description")

    def test_update_call_rejected_when_archived(self):
        self.archive()
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(url, {"description": "new description"})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    def test_update_visibility_rejected_when_archived(self):
        self.archive()
        url = factories.CallFactory.get_protected_url(self.call)
        response = self.client.patch(url, {"reviews_visible_to_submitters": True})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- offerings --------------------------------------------------------

    def test_add_offering_rejected_when_archived(self):
        self.archive()
        url = factories.RequestedOfferingFactory.get_list_url(self.call)
        payload = {
            "offering": marketplace_factories.OfferingFactory.get_public_url(
                self.fixture.offering
            ),
        }
        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_delete_offering_rejected_when_archived(self):
        requested_offering = self.fixture.requested_offering
        self.archive()
        url = factories.RequestedOfferingFactory.get_url(self.call, requested_offering)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- resource templates ----------------------------------------------

    def test_add_resource_template_rejected_when_archived(self):
        requested_offering = self.fixture.requested_offering_accepted
        self.archive()
        url = factories.CallResourceTemplateFactory.get_list_url(self.call)
        payload = {
            "name": "Standard VM Template",
            "requested_offering": factories.RequestedOfferingFactory.get_url(
                call=self.call, requested_offering=requested_offering
            ),
            "attributes": {"cpu": 2, "ram": 4096},
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_delete_resource_template_rejected_when_archived(self):
        template = factories.CallResourceTemplateFactory(
            call=self.call,
            requested_offering=self.fixture.requested_offering_accepted,
        )
        self.archive()
        url = factories.CallResourceTemplateFactory.get_url(self.call, template)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- workflow steps ---------------------------------------------------

    def test_add_workflow_step_rejected_when_archived(self):
        self.archive()
        url = factories.CallWorkflowStepFactory.get_list_url(self.call)
        payload = {"step": "administrative_check", "is_enabled": True}
        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_delete_workflow_step_rejected_when_archived(self):
        step = factories.CallWorkflowStepFactory(
            call=self.call, step="administrative_check"
        )
        self.archive()
        url = factories.CallWorkflowStepFactory.get_url(self.call, step)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- role mappings ----------------------------------------------------

    def test_add_role_mapping_rejected_when_archived(self):
        self.call.proposalprojectrolemapping_set.all().delete()
        self.archive()
        url = factories.ProposalProjectRoleMappingFactory.get_list_url()
        payload = {
            "call": factories.CallFactory.get_protected_url(self.call),
            "project_role": ProjectRole.MEMBER.name,
            "proposal_role": ProposalRole.MEMBER.name,
        }
        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_delete_role_mapping_rejected_when_archived(self):
        self.call.proposalprojectrolemapping_set.all().delete()
        mapping = factories.ProposalProjectRoleMappingFactory(
            call=self.call,
            proposal_role=ProposalRole.MEMBER,
            project_role=ProjectRole.MEMBER,
        )
        self.archive()
        url = factories.ProposalProjectRoleMappingFactory.get_url(mapping)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    # --- rounds and documents --------------------------------------------

    def test_add_round_rejected_when_archived(self):
        self.archive()
        url = factories.RoundFactory.get_list_url(self.call)
        payload = {
            "start_time": "2030-01-01T00:00",
            "cutoff_time": "2030-01-10T00:00",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    def test_attach_documents_rejected_when_archived(self):
        self.archive()
        url = factories.CallFactory.get_protected_url(
            self.call, action="attach_documents"
        )
        payload = {"documents": [{"file": dummy_image()}]}
        response = self.client.post(url, payload, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)


class HasProposalsFieldTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.staff = self.fixture.staff
        self.client.force_authenticate(self.staff)

    def test_has_proposals_true_when_proposal_exists(self):
        # The fixture seeds a proposal on ``call`` via its round.
        self.assertTrue(
            models.Proposal.objects.filter(round__call=self.fixture.call).exists()
        )
        url = factories.CallFactory.get_protected_url(self.fixture.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["has_proposals"])

    def test_has_proposals_false_when_no_proposal(self):
        # ``new_call`` has no rounds and therefore no proposals.
        self.assertFalse(
            models.Proposal.objects.filter(round__call=self.fixture.new_call).exists()
        )
        url = factories.CallFactory.get_protected_url(self.fixture.new_call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.data["has_proposals"])


class ArchivedCallApplicationTest(test.APITestCase):
    """An archived call stays listed for its history but takes no proposals.

    Covers both shapes from github.com/waldur/waldur-homeport/issues/130: an archived
    call with no rounds, and an archived call whose round is otherwise open.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.round = self.fixture.round
        self.empty_call = factories.CallFactory(
            manager=self.fixture.manager, state=CallStates.ARCHIVED
        )

    def archive(self):
        self.call.state = CallStates.ARCHIVED
        self.call.save()

    def list_public_calls(self, **params):
        response = self.client.get(factories.CallFactory.get_public_list_url(), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {item["uuid"] for item in response.data}

    def test_archived_call_without_rounds_is_listed_but_not_open(self):
        self.assertIn(self.empty_call.uuid.hex, self.list_public_calls())
        self.assertNotIn(
            self.empty_call.uuid.hex, self.list_public_calls(has_active_round=True)
        )

    def test_archived_call_with_open_round_is_listed_but_not_open(self):
        self.assertIn(self.call.uuid.hex, self.list_public_calls(has_active_round=True))
        self.archive()
        self.assertIn(self.call.uuid.hex, self.list_public_calls())
        self.assertNotIn(
            self.call.uuid.hex, self.list_public_calls(has_active_round=True)
        )

    def test_proposal_can_not_be_created_in_archived_call_with_open_round(self):
        self.archive()
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(
            factories.ProposalFactory.get_list_url(),
            {"name": "late", "round_uuid": self.round.uuid.hex},
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertFalse(models.Proposal.objects.filter(name="late").exists())

    def test_draft_can_not_be_submitted_after_call_is_archived(self):
        """Archiving leaves drafts behind; their round may still be open."""
        proposal = self.fixture.proposal
        proposal.state = ProposalStates.DRAFT
        proposal.save()
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        self.archive()

        self.client.force_authenticate(self.fixture.proposal_creator)
        response = self.client.post(
            factories.ProposalFactory.get_url(proposal, "submit")
        )

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        proposal.refresh_from_db()
        self.assertEqual(proposal.state, ProposalStates.DRAFT)

    def test_draft_reports_it_can_not_be_submitted_after_call_is_archived(self):
        """The form disables Submit from ``can_submit`` instead of a refusal."""
        proposal = self.fixture.proposal
        proposal.state = ProposalStates.DRAFT
        proposal.save()
        self.archive()

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.ProposalFactory.get_url(proposal))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            response.data["can_submit"],
            {"can_submit": False, "error": str(models.CALL_NOT_ACTIVE_MESSAGE)},
        )
