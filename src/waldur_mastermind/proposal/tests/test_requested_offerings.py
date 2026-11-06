from ddt import data, ddt
from django.core import mail
from django.test.utils import override_settings
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, CustomerRole, OfferingRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import RequestedOfferingStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class RequestedOfferingGetTest(test.APITestCase):
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
class RequestedOfferingAcceptTest(test.APITestCase):
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
        response = self.client.post(self.url, {"reason": "Fully booked."})
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
class RequestedOfferingCancelTest(test.APITestCase):
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
        response = self.client.post(self.url, {"reason": "Fully booked."})
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
        response = self.client.post(self.url, {"reason": "Fully booked."})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(len(mail.outbox), 1)

        email = mail.outbox[0]
        self.assertIn("Offering request rejected", email.subject)
        self.assertIn("Fully booked.", email.body)
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


class RequestedOfferingOfferingManagerTest(test.APITestCase):
    """The offering's own manager answers the requests for that offering."""

    def setUp(self):
        OfferingRole.MANAGER.add_permission(PermissionEnum.ACCEPT_REQUESTED_OFFERING)
        self.fixture = fixtures.ProposalFixture()
        self.requested_offering = self.fixture.requested_offering
        self.manager = structure_factories.UserFactory()
        self.requested_offering.offering.add_user(self.manager, OfferingRole.MANAGER)

    def test_offering_manager_sees_requests_for_their_offering(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(
            factories.RequestedOfferingFactory.get_provider_list_url()
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row["uuid"] for row in response.json()},
            {
                requested.uuid.hex
                for requested in models.RequestedOffering.objects.filter(
                    offering=self.requested_offering.offering
                )
            },
        )
        self.assertIn(
            self.requested_offering.uuid.hex,
            {row["uuid"] for row in response.json()},
        )

    def test_offering_manager_can_accept(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.RequestedOfferingFactory.get_provider_url(
                self.requested_offering, "accept"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.requested_offering.refresh_from_db()
        self.assertEqual(
            self.requested_offering.state, RequestedOfferingStates.ACCEPTED
        )

    def test_offering_manager_can_cancel(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.RequestedOfferingFactory.get_provider_url(
                self.requested_offering, "cancel"
            ),
            {"reason": "Fully booked."},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_manager_of_another_offering_does_not_see_the_request(self):
        other_offering = marketplace_factories.OfferingFactory(
            customer=self.requested_offering.offering.customer
        )
        other_manager = structure_factories.UserFactory()
        other_offering.add_user(other_manager, OfferingRole.MANAGER)
        self.client.force_authenticate(other_manager)

        response = self.client.get(
            factories.RequestedOfferingFactory.get_provider_list_url()
        )
        self.assertEqual(response.json(), [])
        response = self.client.post(
            factories.RequestedOfferingFactory.get_provider_url(
                self.requested_offering, "accept"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_offering_role_without_the_permission_cannot_accept(self):
        OfferingRole.MANAGER.delete_permission(PermissionEnum.ACCEPT_REQUESTED_OFFERING)
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.RequestedOfferingFactory.get_provider_url(
                self.requested_offering, "accept"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
