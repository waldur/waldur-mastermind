from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class RequestedResourceGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedResourceFactory.get_provider_list_url()

    @data(
        "staff",
        "offering_owner",
    )
    def test_request_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_request_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertFalse(len(response.json()))
