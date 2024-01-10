from functools import cached_property

from ddt import data, ddt
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

from .. import PLUGIN_NAME
from . import fixtures


class MarketplaceFixture(fixtures.BookingFixture):
    @cached_property
    def offering(self) -> marketplace_models.Offering:
        return marketplace_factories.OfferingFactory(
            customer=self.customer, type=PLUGIN_NAME
        )

    @cached_property
    def plan(self) -> marketplace_models.PlanComponent:
        return marketplace_factories.PlanFactory(offering=self.offering)

    @cached_property
    def resource(self) -> marketplace_models.Resource:
        return marketplace_factories.ResourceFactory(
            offering=self.offering,
            state=marketplace_models.Resource.States.CREATING,
            project=self.project,
            plan=self.plan,
        )

    @cached_property
    def order(self) -> marketplace_models.Order:
        return marketplace_factories.OrderFactory(
            resource=self.resource,
            offering=self.offering,
            state=marketplace_models.Order.States.EXECUTING,
        )


class OrderGetTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fixture = MarketplaceFixture()

    def test_get_resource_list(self):
        url = reverse("booking-resource-list")
        self.fixture.order
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            marketplace_models.Offering.objects.get(
                uuid=response.data[0]["offering_uuid"]
            ).customer.get_owners()[0],
            self.fixture.owner,
        )

    def test_get_specific_fields(self):
        self.fixture.order.consumer_reviewed_by = self.fixture.staff
        self.fixture.order.save()
        self.fixture.resource.attributes["description"] = "Description"
        self.fixture.resource.save()
        self.client.force_authenticate(self.fixture.owner)
        url = reverse(
            "booking-resource-detail",
            kwargs={"uuid": self.fixture.resource.uuid.hex},
        )
        response = self.client.get(url)
        self.assertTrue(
            self.fixture.order.created_by.uuid.hex in response.data["created_by"]
        )
        self.assertTrue(
            self.fixture.order.consumer_reviewed_by.uuid.hex
            in response.data["consumer_reviewed_by"]
        )
        self.assertEqual("Description", response.data["description"])


@ddt
class OrderAcceptTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fixture = MarketplaceFixture()
        self.fixture.order

        CustomerRole.OWNER.add_permission(PermissionEnum.ACCEPT_BOOKING_REQUEST)
        CustomerRole.MANAGER.add_permission(PermissionEnum.ACCEPT_BOOKING_REQUEST)

    def accept(self, resource, user=None):
        user = user or self.fixture.owner
        self.client.force_authenticate(user)
        url = "{}{}/accept/".format(
            reverse("booking-resource-list"),
            resource.uuid.hex,
        )
        return self.client.post(url)

    @data("staff", "owner", "offering_owner", "offering_service_manager")
    def test_user_can_accept_his_resource(self, user):
        response = self.accept(self.fixture.resource, user=getattr(self.fixture, user))
        self.assertEqual(status.HTTP_200_OK, response.status_code, response.data)

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.OK
        )

        self.fixture.order.refresh_from_db()
        self.assertEqual(self.fixture.order.state, marketplace_models.Order.States.DONE)

    def test_owner_cannot_accept_other_owners_resources(self):
        response = self.accept(MarketplaceFixture().resource)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)

    def test_creator_cannot_accept_his_resource(self):
        response = self.accept(self.fixture.resource, self.fixture.order.created_by)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    def test_when_order_is_accepted_resource_plan_period_is_created(self):
        response = self.accept(self.fixture.resource)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertTrue(
            marketplace_models.ResourcePlanPeriod.objects.filter(
                resource=self.fixture.resource, plan=self.fixture.resource.plan
            ).exists()
        )


class OrderRejectTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fixture = MarketplaceFixture()
        self.fixture.order
        CustomerRole.OWNER.add_permission(PermissionEnum.REJECT_BOOKING_REQUEST)

    def reject(self, resource, user=None):
        user = user or self.fixture.owner
        self.client.force_authenticate(user)
        url = "{}{}/reject/".format(
            reverse("booking-resource-list"),
            resource.uuid.hex,
        )
        return self.client.post(url)

    def test_owner_can_reject_his_resource(self):
        response = self.reject(self.fixture.resource)
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.TERMINATED
        )

        self.fixture.order.refresh_from_db()
        self.assertEqual(
            self.fixture.order.state,
            marketplace_models.Order.States.CANCELED,
        )

    def test_owner_cannot_reject_other_owners_resources(self):
        response = self.reject(MarketplaceFixture().resource)
        self.assertEqual(status.HTTP_404_NOT_FOUND, response.status_code)

    def test_creator_can_reject_his_resource(self):
        response = self.reject(self.fixture.resource, self.fixture.order.created_by)
        self.assertEqual(status.HTTP_200_OK, response.status_code)

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.TERMINATED
        )

        self.fixture.order.refresh_from_db()
        self.assertEqual(
            self.fixture.order.state,
            marketplace_models.Order.States.CANCELED,
        )


@ddt
class ResourceGetTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.BookingFixture()
        self.resource_1 = self.fixture.resource
        self.fixture_2 = fixtures.BookingFixture()
        self.resource_2 = self.fixture_2.resource
        self.fixture_3 = fixtures.BookingFixture()
        self.resource_3 = self.fixture_3.resource
        self.resource_1.attributes = {
            "schedules": [
                {
                    "start": "2020-01-12T02:00:00+03:00",
                    "end": "2020-01-15T02:00:00+03:00",
                    "id": "1",
                }
            ]
        }
        self.resource_1.save()

        self.resource_2.attributes = {
            "schedules": [
                {
                    "start": "2020-02-12T02:00:00+03:00",
                    "end": "2020-02-15T02:00:00+03:00",
                    "id": "2",
                }
            ]
        }
        self.resource_2.save()

        self.resource_3.attributes = {
            "schedules": [
                {
                    "start": "2020-03-12T02:00:00+03:00",
                    "end": "2020-03-15T02:00:00+03:00",
                    "id": "3",
                }
            ]
        }
        self.resource_3.save()
        self.url = reverse("booking-resource-list")

    def test_ordering_by_schedules(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + "?o=schedules")
        self.assertEqual(len(response.data), 3)
        self.assertEqual(response.data[0]["uuid"], self.resource_1.uuid.hex)
        self.assertEqual(response.data[2]["uuid"], self.resource_3.uuid.hex)

        response = self.client.get(self.url + "?o=-schedules")
        self.assertEqual(len(response.data), 3)
        self.assertEqual(response.data[0]["uuid"], self.resource_3.uuid.hex)
        self.assertEqual(response.data[2]["uuid"], self.resource_1.uuid.hex)

    @data(
        "staff",
    )
    def test_user_can_get_all_resources(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(3, len(response.data))

    @data("offering_owner", "offering_service_manager")
    def test_user_can_get_only_his_resources(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data(
        "admin",
    )
    def test_user_cannot_get_resources(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_connected_customer_uuid_filter(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            self.url,
            {"connected_customer_uuid": self.resource_1.offering.customer.uuid.hex},
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(1, len(response.data))
        self.assertEqual(self.resource_1.uuid.hex, response.data[0]["uuid"])
        self.assertEqual(
            self.resource_1.offering.customer.uuid, response.data[0]["provider_uuid"]
        )

        response = self.client.get(
            self.url,
            {"connected_customer_uuid": self.resource_3.project.customer.uuid.hex},
        )
        self.assertEqual(1, len(response.data))
        self.assertEqual(self.resource_3.uuid.hex, response.data[0]["uuid"])
        self.assertEqual(
            self.resource_3.project.customer.uuid, response.data[0]["customer_uuid"]
        )
