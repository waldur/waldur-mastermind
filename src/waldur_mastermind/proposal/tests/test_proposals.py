from ddt import data, ddt
from rest_framework import status, test

from waldur_core.media.utils import dummy_image
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class ProposalGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_list_url()

    @data(
        "staff",
        "owner",
        "customer_support",
        "proposal_creator",
    )
    def test_proposal_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("user")
    def test_proposal_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(len(response.json()))


@ddt
class ProposalCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_list_url()

    @data(
        "staff",
        "owner",
        "customer_support",
        "user",
    )
    def test_user_can_add_proposal(self, user):
        response = self.create_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Proposal.objects.filter(uuid=response.data["uuid"]).exists()
        )

    def create_proposal(self, user, **kwargs):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "name": "new",
            "round_uuid": self.fixture.round.uuid.hex,
            "duration_in_days": 10,
        }
        payload.update(kwargs)

        return self.client.post(self.url, payload)


@ddt
class ProposalUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.url = factories.ProposalFactory.get_url(self.proposal)

    @data(
        "staff",
        "proposal_creator",
    )
    def test_user_can_update_proposal(self, user):
        response = self.update_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "user",
    )
    def test_user_can_not_update_proposal(self, user):
        response = self.update_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "owner",
        "customer_support",
    )
    def test_customer_user_can_not_update_proposal(self, user):
        response = self.update_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "proposal_creator")
    def test_upload_supporting_documentation(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "project_summary": "new summary",
            "project_duration": 10,
            "project_is_confidential": True,
            "project_has_civilian_purpose": True,
            "supporting_documentation": [
                {"file": dummy_image()},
                {"file": dummy_image()},
            ],
        }

        response = self.client.patch(self.url, payload, format="multipart")
        proposal = models.Proposal.objects.get(uuid=self.proposal.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(proposal.proposaldocumentation_set.all()), 2)

        payload = {
            "supporting_documentation": [],
        }
        response = self.client.patch(self.url, payload, format="multipart")
        proposal = models.Proposal.objects.get(uuid=self.proposal.uuid)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(proposal.proposaldocumentation_set.all()), 0)

    @data(
        "staff",
        "proposal_creator",
    )
    def test_user_can_not_update_not_draft_proposal(self, user):
        self.proposal.state = models.Proposal.States.IN_REVIEW
        self.proposal.save()
        response = self.update_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def update_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "name": "new",
            "round_uuid": self.fixture.round.uuid.hex,
            "duration_in_days": 10,
        }
        response = self.client.patch(self.url, payload)
        self.proposal.refresh_from_db()
        return response


@ddt
class ProposalDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.url = factories.ProposalFactory.get_url(self.proposal)

    @data(
        "staff",
        "proposal_creator",
    )
    def test_user_can_delete_round(self, user):
        response = self.delete_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data(
        "owner",
        "customer_support",
    )
    def test_customer_user_can_not_delete_round(self, user):
        response = self.delete_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("user")
    def test_user_can_not_delete_round(self, user):
        response = self.delete_proposal(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def delete_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)


@ddt
class ActionTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.url = factories.ProposalFactory.get_url(self.proposal, "submit")

    @data(
        "staff",
        "proposal_creator",
    )
    def test_user_can_submit_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.proposal.refresh_from_db()
        self.assertTrue(self.proposal.state, models.Proposal.States.SUBMITTED)

    @data(
        "owner",
        "customer_support",
    )
    def test_user_can_not_submit_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class RequestedResourceGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedResourceFactory.get_list_url(
            self.fixture.proposal
        )

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_requested_resource_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("user")
    def test_call_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class RequestedResourceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.requested_offering_accepted = self.fixture.requested_offering_accepted
        self.requested_offering = self.fixture.requested_offering
        self.url = factories.RequestedResourceFactory.get_list_url(self.proposal)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_add_resource_to_proposal(self, user):
        response = self.add_resource(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.RequestedResource.objects.filter(uuid=response.data["uuid"]).exists()
        )

    @data("user")
    def test_user_can_not_add_resource_to_proposal(self, user):
        response = self.add_resource(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_not_add_resource_to_not_draft_proposal(self, user):
        self.proposal.state = models.Proposal.States.IN_REVIEW
        self.proposal.save()
        response = self.add_resource(user)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_not_add_if_requested_offering_is_not_accepted(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        payload = {
            "requested_offering": self.requested_offering.uuid.hex,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def add_resource(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {"requested_offering_uuid": self.requested_offering_accepted.uuid.hex}

        return self.client.post(self.url, payload)


@ddt
class RequestedResourceUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.proposal = self.fixture.proposal
        self.url = factories.RequestedResourceFactory.get_url(
            self.proposal, self.requested_resource
        )

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_update_requested_resource(self, user):
        response = self.update_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("user")
    def test_user_can_not_update_requested_resource(self, user):
        response = self.update_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_not_update_not_draft_requested_resource(self, user):
        self.proposal.state = models.Proposal.States.IN_REVIEW
        self.proposal.save()
        response = self.update_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def update_requested_resource(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "description": "description",
            "requested_offering_uuid": self.requested_resource.requested_offering.uuid.hex,
        }
        response = self.client.patch(self.url, payload)
        self.requested_resource.refresh_from_db()
        return response


@ddt
class RequestedResourceDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.proposal = self.fixture.proposal
        self.url = factories.RequestedResourceFactory.get_url(
            self.proposal, self.requested_resource
        )

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_delete_requested_resource(self, user):
        response = self.delete_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("user")
    def test_user_can_not_delete_requested_resource(self, user):
        response = self.delete_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_not_delete_not_draft_requested_resource(self, user):
        self.proposal.state = models.Proposal.States.IN_REVIEW
        self.proposal.save()
        response = self.delete_requested_resource(user)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def delete_requested_resource(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)
