from rest_framework import status, test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_packages import PLUGIN_NAME
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages.tests import fixtures as package_fixtures


class PackageOrderTest(test.APITransactionTestCase):
    def test_when_order_item_is_approved_openstack_package_is_created(self):
        # Arrange
        fixture = package_fixtures.PackageFixture()
        offering = marketplace_factories.OfferingFactory(scope=fixture.openstack_service_settings,
                                                         type=PLUGIN_NAME)
        order = marketplace_factories.OrderFactory(
            state=marketplace_models.Order.States.REQUESTED_FOR_APPROVAL)
        plan = marketplace_factories.PlanFactory(scope=fixture.openstack_template)
        attributes = dict(
            name='My first VPC',
            description='Database cluster',
            user_username='admin_user',
        )
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=offering,
            attributes=attributes,
            plan=plan
        )

        # Act
        url = marketplace_factories.OrderFactory.get_url(order, 'set_state_executing')
        self.client.force_login(fixture.staff)
        response = self.client.post(url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order_item.refresh_from_db()
        self.assertTrue(isinstance(order_item.scope, package_models.OpenStackPackage))
