"""Text people write shows in event messages as written, and never breaks the
request that changes it."""

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories, fixtures

TEXTS = (
    "{user_agent.__class__.__mro__}",
    "{user_agent:>200000}",
    "{ip_address} {user_agent}",
    "AA{{BB}}CC",
    "price {",
    "{nosuchkey}",
)


def last_message(event_type):
    return Event.objects.filter(event_type=event_type).latest("created").message


@ddt
class AccessSubnetDescriptionTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_ACCESS_SUBNET)
        self.subnet = factories.AccessSubnetFactory(
            customer=self.fixture.customer, inet="1.1.1.1/32", description="test"
        )
        self.client.force_authenticate(self.fixture.owner)

    @data(*TEXTS)
    def test_description_shows_as_written(self, text):
        response = self.client.patch(
            factories.AccessSubnetFactory.get_url(self.subnet),
            {"inet": "1.1.1.1/32", "description": text},
            format="json",
            HTTP_USER_AGENT="Browser/1.0",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        message = last_message(EventType.ACCESS_SUBNET_UPDATE_SUCCEEDED)
        self.assertIn(f"to '{text}'", message)
        self.assertNotIn("Browser/1.0", message)
        self.assertNotIn("<class", message)

    def test_organization_name_shows_as_written_on_create(self):
        self.fixture.customer.name = "Org {nosuchkey}"
        self.fixture.customer.save()

        factories.AccessSubnetFactory(customer=self.fixture.customer, inet="2.2.2.2/32")

        self.assertIn(
            "Access subnet Org {nosuchkey} | 2.2.2.2/32 has been created.",
            last_message(EventType.ACCESS_SUBNET_CREATION_SUCCEEDED),
        )


@ddt
class CustomerUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.client.force_authenticate(self.fixture.staff)

    # Braces are legal in the local part of an email address.
    @data("AA{{BB}}CC@example.com", "billing{@example.com", "{ip_address}@example.com")
    def test_invoice_emails_show_as_written(self, email):
        response = self.client.patch(
            factories.CustomerFactory.get_url(self.fixture.customer),
            {"invoice_emails": email},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn(
            f"to '{email}'", last_message(EventType.CUSTOMER_UPDATE_SUCCEEDED)
        )


class PermissionReviewTest(test.APITestCase):
    def test_project_name_shows_as_written_on_close(self):
        name = "AA{{BB}}CC {nosuchkey} {"
        review = factories.ProjectPermissionReviewFactory(
            project=factories.ProjectFactory(name=name)
        )
        self.client.force_authenticate(factories.UserFactory(is_staff=True))

        response = self.client.post(
            factories.ProjectPermissionReviewFactory.get_url(review, "close")
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            last_message(EventType.PROJECT_PERMISSION_REVIEW_CLOSED),
            f"Project permission review has been closed for project {name}.",
        )
