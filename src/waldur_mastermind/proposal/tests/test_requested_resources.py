from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class RequestedResourceGetTest(test.APITestCase):
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


class RequestedResourceOfferingFilterTest(test.APITestCase):
    """The offering is reached through RequestedOffering, not a direct FK.

    Filtering or ordering on the shorter ``offering__…`` path raised FieldError,
    which surfaced as a 500 rather than a validation error.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.url = factories.RequestedResourceFactory.get_provider_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_filter_by_offering_uuid_returns_the_matching_row(self):
        response = self.client.get(
            self.url, {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["uuid"] for row in response.json()],
            [self.requested_resource.uuid.hex],
        )

    def test_filter_by_offering_uuid_excludes_other_offerings(self):
        other = marketplace_factories.OfferingFactory()
        response = self.client.get(self.url, {"offering_uuid": other.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), [])

    def test_ordering_by_offering_name_is_accepted(self):
        response = self.client.get(self.url, {"o": "offering__name"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
