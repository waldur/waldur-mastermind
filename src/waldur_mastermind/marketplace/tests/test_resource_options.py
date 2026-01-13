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
class ResourceUpdateOptionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        options = {
            "email": {
                "type": "string",
                "label": "email",
                "default": "user@example.com",
                "required": False,
            }
        }
        self.fixture.offering.resource_options = {"options": options}
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()
        self.url = factories.ResourceFactory.get_url(self.resource, "update_options")
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)

    def make_request(self, user, payload=None, custom_url=None):
        url = custom_url or self.url
        self.client.force_authenticate(user)
        payload = payload or {"options": {"email": "order@example.com"}}
        return self.client.post(url, payload)

    @data(
        "staff",
        "owner",
    )
    def test_user_can_update_resource_options(self, user):
        response = self.make_request(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["email"], "order@example.com")

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
            order.attributes.get("new_options"), {"email": "order@example.com"}
        )

        order.set_state_executing()
        order.save()
        marketplace_utils.process_order(order, self.fixture.owner)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, {"email": "order@example.com"})
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
        self.assertEqual(self.resource.options["email"], "order@example.com")

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
