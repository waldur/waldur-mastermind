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

    def create_proposal(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "name": "new",
            "round_uuid": self.fixture.round.uuid.hex,
            "duration_in_days": 10,
        }

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
