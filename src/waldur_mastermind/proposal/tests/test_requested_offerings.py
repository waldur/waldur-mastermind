from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class RequestedOfferingGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RequestedOfferingFactory.get_provider_list_url()

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
    def test_round_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertFalse(len(response.json()))


@ddt
class RequestedOfferingAcceptTest(test.APITransactionTestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.ACCEPT_REQUESTED_OFFERING)
        self.fixture = fixtures.ProposalFixture()
        self.requested_offering = self.fixture.requested_offering
        self.url = factories.RequestedOfferingFactory.get_provider_url(
            self.requested_offering, "accept"
        )

    @data(
        "staff",
        "offering_owner",
    )
    def test_user_can_accept(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.requested_offering.refresh_from_db()
        self.assertEqual(
            self.requested_offering.state, models.RequestedOffering.States.ACCEPTED
        )

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_accept(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class RequestedOfferingCancelTest(test.APITransactionTestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.ACCEPT_REQUESTED_OFFERING)
        self.fixture = fixtures.ProposalFixture()
        self.requested_offering = self.fixture.requested_offering
        self.url = factories.RequestedOfferingFactory.get_provider_url(
            self.requested_offering, "cancel"
        )

    @data(
        "staff",
        "offering_owner",
    )
    def test_user_can_cancel(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.requested_offering.refresh_from_db()
        self.assertEqual(
            self.requested_offering.state, models.RequestedOffering.States.CANCELED
        )

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_cancel(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
