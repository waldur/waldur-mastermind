from django.test.utils import override_settings
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_packages import PLUGIN_NAME
from waldur_mastermind.packages.tests import factories as package_factories
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_openstack.openstack import models as openstack_models


class PackageOrderTest(test.APITransactionTestCase):
    def test_when_order_is_created_items_are_validated(self):
        response = self.create_order()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_mandatory_attributes_are_checked(self):
        response = self.create_order(dict(user_username=None))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('user_username' in response.data)

    def create_order(self, add_attributes=None):
        fixture = package_fixtures.PackageFixture()
        project_url = structure_factories.ProjectFactory.get_url(fixture.project)

        offering = marketplace_factories.OfferingFactory(
            scope=fixture.openstack_service_settings,
            type=PLUGIN_NAME,
            state=marketplace_models.Offering.States.ACTIVE,
        )
        offering_url = marketplace_factories.OfferingFactory.get_url(offering)

        plan = marketplace_factories.PlanFactory(scope=fixture.openstack_template, offering=offering)
        plan_url = marketplace_factories.PlanFactory.get_url(plan)

        # Create SPL
        fixture.openstack_spl

        attributes = dict(
            name='My first VPC',
            description='Database cluster',
            user_username='admin_user',
        )
        if add_attributes:
            attributes.update(add_attributes)

        payload = {
            'project': project_url,
            'items': [
                {
                    'offering': offering_url,
                    'plan': plan_url,
                    'attributes': attributes,
                },
            ]
        }

        self.client.force_login(fixture.staff)
        url = marketplace_factories.OrderFactory.get_list_url()
        return self.client.post(url, payload)

    @override_settings(ALLOWED_HOSTS=['localhost'])
    def test_when_order_item_is_approved_openstack_tenant_is_created(self):
        # Arrange
        fixture = package_fixtures.PackageFixture()
        offering = marketplace_factories.OfferingFactory(
            scope=fixture.openstack_service_settings,
            type=PLUGIN_NAME
        )
        order = marketplace_factories.OrderFactory(
            state=marketplace_models.Order.States.REQUESTED_FOR_APPROVAL,
            project=fixture.project,
        )
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

        serialized_order = core_utils.serialize_instance(order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        # Assert
        order_item.refresh_from_db()
        self.assertTrue(isinstance(order_item.scope, openstack_models.Tenant))

    def test_order_item_set_state_done(self):
        openstack_package = package_factories.OpenStackPackageFactory()
        order_item = marketplace_factories.OrderItemFactory(scope=openstack_package)
        order_item.set_state('executing')
        order_item.order.state = marketplace_models.Order.States.EXECUTING
        order_item.order.save()
        openstack_package.tenant.state = openstack_models.Tenant.States.CREATION_SCHEDULED
        openstack_package.tenant.save()
        openstack_package.tenant.state = openstack_models.Tenant.States.OK
        openstack_package.tenant.save()
        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)
