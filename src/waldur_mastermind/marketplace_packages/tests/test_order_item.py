from django.core.exceptions import ObjectDoesNotExist
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


class TenantCreateTest(test.APITransactionTestCase):
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
        self.assertTrue(isinstance(order_item.resource.scope, openstack_models.Tenant))

    def test_order_item_set_state_done(self):
        openstack_package = package_factories.OpenStackPackageFactory()
        resource = marketplace_factories.ResourceFactory(scope=openstack_package)

        order_item = marketplace_factories.OrderItemFactory(resource=resource)
        order_item.set_state_executing()
        order_item.save()

        order_item.order.approve()
        order_item.order.save()

        openstack_package.tenant.state = openstack_models.Tenant.States.CREATING
        openstack_package.tenant.save()

        openstack_package.tenant.state = openstack_models.Tenant.States.OK
        openstack_package.tenant.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)

        order_item.resource.refresh_from_db()
        self.assertEqual(order_item.resource.state, marketplace_models.Resource.States.OK)

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)


class TenantDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = package_fixtures.PackageFixture()
        self.openstack_package = self.fixture.openstack_package
        self.offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.openstack_package, offering=self.offering)
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            type=marketplace_models.RequestTypeMixin.Types.TERMINATE,
        )

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.EXECUTING)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.TERMINATING)
        self.assertEqual(self.openstack_package.tenant.state, openstack_models.Tenant.States.DELETION_SCHEDULED)

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.openstack_package.tenant.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertRaises(ObjectDoesNotExist, self.openstack_package.tenant.refresh_from_db)

    def trigger_deletion(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.openstack_package.tenant.refresh_from_db()
