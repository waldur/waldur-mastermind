from ddt import data, ddt
from django.core import mail
from django.test.utils import override_settings
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal.enums import RequestedOfferingStates
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
    def test_request_should_not_be_visible(self, user):
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
            self.requested_offering.state, RequestedOfferingStates.ACCEPTED
        )

    @override_settings(task_always_eager=True)
    @data(
        "staff",
        "offering_owner",
    )
    def test_notification_sent_on_accept(self, user):
        structure_factories.NotificationFactory(
            key="proposal.requested_offering_decision",
        )
        self.requested_offering.call.add_user(
            self.fixture.call_manager, CallRole.MANAGER
        )

        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(len(mail.outbox), 1)

        email = mail.outbox[0]
        self.assertIn("Offering request accepted", email.subject)
        self.assertIn(self.requested_offering.offering.name, email.body)
        self.assertIn(self.fixture.call.name, email.body)
        self.assertIn(self.requested_offering.offering.customer.name, email.body)

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
            self.requested_offering.state, RequestedOfferingStates.CANCELED
        )

    @override_settings(task_always_eager=True)
    @data(
        "staff",
        "offering_owner",
    )
    def test_notification_sent_on_cancel(self, user):
        structure_factories.NotificationFactory(
            key="proposal.requested_offering_decision",
        )
        self.requested_offering.call.add_user(
            self.fixture.call_manager, CallRole.MANAGER
        )

        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(len(mail.outbox), 1)

        email = mail.outbox[0]
        self.assertIn("Offering request canceled", email.subject)
        self.assertIn(self.requested_offering.offering.name, email.body)
        self.assertIn(self.fixture.call.name, email.body)
        self.assertIn(self.requested_offering.offering.customer.name, email.body)

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
