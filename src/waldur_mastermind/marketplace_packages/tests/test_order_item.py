from rest_framework import status, test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages.tests import fixtures as package_fixtures


class PackageOrderTest(test.APITransactionTestCase):
    def test_when_order_item_is_approved_openstack_package_is_created(self):
        # Arrange
        fixture = package_fixtures.PackageFixture()
        template = fixture.openstack_template
        offering = marketplace_factories.OfferingFactory(scope=template)
        order = marketplace_factories.OrderFactory(
            state=marketplace_models.Order.States.REQUESTED_FOR_APPROVAL)
        attributes = dict(
            name='My first VPC',
            description='Database cluster',
            user_username='admin_user',
        )
        order_item = marketplace_factories.OrderItemFactory(
            order=order, offering=offering, attributes=attributes)

        # Act
        url = marketplace_factories.OrderFactory.get_url(order, 'set_state_executing')
        self.client.force_login(fixture.staff)
        response = self.client.post(url)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        order_item.refresh_from_db()
        self.assertTrue(isinstance(order_item.scope, package_models.OpenStackPackage))
