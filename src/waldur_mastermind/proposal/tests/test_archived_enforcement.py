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
from waldur_mastermind.proposal.enums import CallStates
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
