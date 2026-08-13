from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


@ddt
class ResourceUpdateOptionsTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        options = {
            "storage": {
                "type": "integer",
                "label": "storage",
                "required": True,
            },
            "soft_limit": {
                "type": "integer",
                "label": "soft_limit",
                "required": False,
            },
            "hard_limit": {
                "type": "integer",
                "label": "hard_limit",
                "required": False,
                "validators": [
                    {
                        "type": "gte",
                        "target_field": "soft_limit",
                    }
                ],
            },
        }
        self.fixture.offering.resource_options = {"options": options}
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.url = factories.ResourceFactory.get_url(self.resource, "update_options")
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        # Offerings configured to create orders on option change route the
        # change through an order, which needs order creation rights.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)

    def make_request(self, user, payload=None, custom_url=None):
        url = custom_url or self.url
        self.client.force_authenticate(user)
        payload = payload or {
            "options": {"storage": 1024, "soft_limit": 100, "hard_limit": 200}
        }
        return self.client.post(url, payload)

    @data(
        "staff",
        "owner",
    )
    def test_user_can_update_resource_options(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["storage"], 1024)
        self.assertEqual(self.resource.options["soft_limit"], 100)
        self.assertEqual(self.resource.options["hard_limit"], 200)

    def test_create_order_when_offering_requires_order_for_option_change(self):
        self.fixture.offering.plugin_options = {
            "create_orders_on_resource_option_change": True
        }
        self.fixture.offering.save()
        response = self.make_request(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, None)
        self.assertTrue(
            models.Order.objects.filter(uuid=response.data["order_uuid"]).exists()
        )
        order = models.Order.objects.filter(uuid=response.data["order_uuid"]).get()
        self.assertEqual(
            order.attributes.get("new_options"),
            {"storage": 1024, "soft_limit": 100, "hard_limit": 200},
        )

        order.set_state_executing()
        order.save()
        marketplace_utils.process_order(order, self.fixture.owner)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["storage"], 1024)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.DONE)
        self.assertEqual(self.resource.state, models.Resource.States.OK)

    @data("admin")
    def test_user_can_not_update_resource_options(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("offering_owner")
    def test_service_provider_can_update_resource_options(self, user):
        url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, "update_options"
        )

        response = self.make_request(getattr(self.fixture, user), custom_url=url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["storage"], 1024)

    def test_update_options_fails_for_erred_resource(self):
        self.resource.state = ResourceStates.ERRED
        self.resource.save()
        response = self.make_request(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_update_options_fails_for_updating_resource(self):
        self.resource.state = ResourceStates.UPDATING
        self.resource.save()
        response = self.make_request(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_update_options_fails_for_terminated_resource(self):
        self.resource.state = ResourceStates.TERMINATED
        self.resource.save()
        response = self.make_request(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_update_options_fails_if_pending_order_exists(self):
        factories.OrderFactory(
            resource=self.resource,
            state=OrderStates.PENDING_CONSUMER,
            attributes={"new_options": {"storage": 512}},
        )
        response = self.make_request(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.data["detail"],
            "There's a pending order for changing resource options.",
        )

    def test_service_provider_can_update_resource_options_during_approval(self):
        # 1. Offering requires order for option change
        self.fixture.offering.plugin_options = {
            "create_orders_on_resource_option_change": True
        }
        self.fixture.offering.save()

        # 2. Consumer creates an order
        response = self.make_request(
            self.fixture.owner,
            payload={
                "options": {"storage": 1024, "soft_limit": 100, "hard_limit": 200}
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])
        self.assertEqual(order.attributes["new_options"]["hard_limit"], 200)

        # 3. Provider approves and updates options
        url = factories.OrderFactory.get_url(order, "approve_by_provider")
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            url,
            {
                "attributes": {
                    "new_options": {
                        "storage": 1024,
                        "soft_limit": 100,
                        "hard_limit": 300,
                    }
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        order.refresh_from_db()
        self.assertEqual(order.attributes["new_options"]["hard_limit"], 300)

        # 4. Process order and verify resource options
        marketplace_utils.process_order(order, self.fixture.offering_owner)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["hard_limit"], 300)

    def test_service_provider_can_not_update_resource_options_with_invalid_data(self):
        # 1. Offering requires order for option change
        self.fixture.offering.plugin_options = {
            "create_orders_on_resource_option_change": True
        }
        self.fixture.offering.save()

        # 2. Consumer creates an order
        response = self.make_request(
            self.fixture.owner,
            payload={
                "options": {"storage": 1024, "soft_limit": 100, "hard_limit": 200}
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order = models.Order.objects.get(uuid=response.data["order_uuid"])

        # 3. Provider approves with invalid options (hard_limit < soft_limit)
        url = factories.OrderFactory.get_url(order, "approve_by_provider")
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            url,
            {
                "attributes": {
                    "new_options": {
                        "storage": 1024,
                        "soft_limit": 500,
                        "hard_limit": 300,
                    }
                }
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
