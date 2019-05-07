from ddt import data, ddt
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack.tests.utils import BaseOpenStackTest
from waldur_mastermind.packages import models as package_models
from waldur_mastermind.packages.tests import factories as package_factories
from waldur_mastermind.packages.tests import fixtures as package_fixtures
from waldur_mastermind.packages.tests import utils as package_utils
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.tests import factories as openstack_tenant_factories
from waldur_openstack.openstack_tenant.tests import fixtures as openstack_tenant_fixtures

from .. import INSTANCE_TYPE, PACKAGE_TYPE, VOLUME_TYPE


@ddt
class TenantCreateTest(BaseOpenStackTest):

    @data('staff', 'owner', 'manager', 'admin')
    def test_when_order_is_created_items_are_validated(self, user):
        response = self.create_order(user=user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_mandatory_attributes_are_checked(self):
        response = self.create_order(dict(user_username=None))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('user_username' in response.data)

    def create_order(self, add_attributes=None, user='staff'):
        fixture = package_fixtures.PackageFixture()
        project_url = structure_factories.ProjectFactory.get_url(fixture.project)

        offering = marketplace_factories.OfferingFactory(
            scope=fixture.openstack_service_settings,
            type=PACKAGE_TYPE,
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

        self.client.force_login(getattr(fixture, user))
        url = marketplace_factories.OrderFactory.get_list_url()
        return self.client.post(url, payload)

    def test_when_order_item_is_approved_openstack_tenant_is_created(self):
        # Arrange
        fixture = package_fixtures.PackageFixture()
        offering = marketplace_factories.OfferingFactory(
            scope=fixture.openstack_service_settings,
            type=PACKAGE_TYPE
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
        tenant = package_factories.OpenStackPackageFactory().tenant
        resource = marketplace_factories.ResourceFactory(scope=tenant)

        order_item = marketplace_factories.OrderItemFactory(resource=resource)
        order_item.set_state_executing()
        order_item.save()

        order_item.order.approve()
        order_item.order.save()

        tenant.state = openstack_models.Tenant.States.CREATING
        tenant.save()

        tenant.state = openstack_models.Tenant.States.OK
        tenant.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)

        order_item.resource.refresh_from_db()
        self.assertEqual(order_item.resource.state, marketplace_models.Resource.States.OK)

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)


class TenantMutateTest(test.APITransactionTestCase):
    def setUp(self):
        super(TenantMutateTest, self).setUp()
        self.fixture = package_fixtures.PackageFixture()
        self.tenant = self.fixture.openstack_package.tenant
        self.offering = marketplace_factories.OfferingFactory(type=PACKAGE_TYPE)
        self.plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            scope=self.fixture.openstack_template
        )
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant,
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
        )
        self.order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )


class TenantDeleteTest(TenantMutateTest):
    def setUp(self):
        super(TenantDeleteTest, self).setUp()
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            type=marketplace_models.RequestTypeMixin.Types.TERMINATE,
        )

    def test_deletion_is_scheduled(self):
        self.trigger_deletion()
        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.EXECUTING)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.TERMINATING)
        self.assertEqual(self.tenant.state, openstack_models.Tenant.States.DELETION_SCHEDULED)

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.tenant.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertRaises(ObjectDoesNotExist, self.tenant.refresh_from_db)

    def trigger_deletion(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.tenant.refresh_from_db()


@ddt
class TenantUpdateTest(TenantMutateTest):
    def setUp(self):
        super(TenantUpdateTest, self).setUp()
        self.resource.state = marketplace_models.Resource.States.OK
        self.resource.save()

        self.new_template = package_factories.PackageTemplateFactory(
            service_settings=self.fixture.openstack_service_settings
        )
        self.new_plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            scope=self.new_template,
        )
        self.order_item = marketplace_factories.OrderItemFactory(
            resource=self.resource,
            plan=self.new_plan,
            type=marketplace_models.RequestTypeMixin.Types.UPDATE,
        )
        self.package = self.fixture.openstack_package

    @data('staff', 'owner', 'manager', 'admin')
    def test_user_can_create_order_item(self, user):
        self.order_item.delete()
        url = marketplace_factories.ResourceFactory.get_url(resource=self.resource, action='switch_plan')
        payload = {
            'plan': marketplace_factories.PlanFactory.get_url(self.new_plan)
        }
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_update_is_scheduled(self):
        self.trigger_update()
        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.EXECUTING)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.UPDATING)
        self.assertEqual(self.resource.plan, self.plan)

        package_utils.run_openstack_package_change_executor(self.package, self.new_template)
        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.OK)
        self.assertEqual(self.resource.plan, self.new_plan)

        package = package_models.OpenStackPackage.objects.get(tenant=self.tenant)
        self.assertEqual(package.template, self.new_template)

    def test_update_is_completed(self):
        self.trigger_update()

        self.tenant.schedule_updating()
        self.tenant.save()

        self.tenant.begin_updating()
        self.tenant.save()

        self.tenant.set_ok()
        self.tenant.save()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.OK)
        self.assertEqual(self.resource.plan, self.new_plan)

    def trigger_update(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.tenant.refresh_from_db()


class InstanceCreateTest(test.APITransactionTestCase):
    def test_instance_is_created_when_order_item_is_processed(self):
        order_item = self.trigger_instance_creation()
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.EXECUTING)
        self.assertTrue(openstack_tenant_models.Instance.objects.filter(name='Virtual machine').exists())

    def test_request_payload_is_validated(self):
        order_item = self.trigger_instance_creation(system_volume_size=100)
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)

    def test_instance_state_is_synchronized(self):
        order_item = self.trigger_instance_creation()
        instance = order_item.resource.scope

        instance.begin_creating()
        instance.save()

        instance.set_ok()
        instance.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)

        order_item.resource.refresh_from_db()
        self.assertEqual(order_item.resource.state, marketplace_models.Resource.States.OK)

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)

    def trigger_instance_creation(self, **kwargs):
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        service_settings = fixture.openstack_tenant_service_settings

        image = openstack_tenant_factories.ImageFactory(
            settings=service_settings,
            min_disk=10240,
            min_ram=1024
        )
        flavor = openstack_tenant_factories.FlavorFactory(settings=service_settings)

        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(fixture.subnet)
        attributes = {
            'flavor': openstack_tenant_factories.FlavorFactory.get_url(flavor),
            'image': openstack_tenant_factories.ImageFactory.get_url(image),
            'name': 'Virtual machine',
            'system_volume_size': image.min_disk,
            'internal_ips_set': [{'subnet': subnet_url}],
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(type=INSTANCE_TYPE, scope=service_settings)
        # Ensure that SPL exists
        fixture.spl
        order = marketplace_factories.OrderFactory(
            project=fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        order_item = marketplace_factories.OrderItemFactory(
            offering=offering,
            attributes=attributes,
            order=order,
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        order_item.refresh_from_db()
        return order_item


class InstanceDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance
        self.offering = marketplace_factories.OfferingFactory(type=INSTANCE_TYPE)
        self.resource = marketplace_factories.ResourceFactory(scope=self.instance, offering=self.offering)
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
        self.assertEqual(self.instance.state, openstack_tenant_models.Instance.States.DELETION_SCHEDULED)

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.instance.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertRaises(ObjectDoesNotExist, self.instance.refresh_from_db)

    def trigger_deletion(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.instance.refresh_from_db()


class VolumeCreateTest(test.APITransactionTestCase):
    def test_volume_is_created_when_order_item_is_processed(self):
        order_item = self.trigger_volume_creation()
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.EXECUTING)
        self.assertTrue(openstack_tenant_models.Volume.objects.filter(name='Volume').exists())

    def test_request_payload_is_validated(self):
        order_item = self.trigger_volume_creation(size=100)
        self.assertEqual(order_item.state, marketplace_models.OrderItem.States.ERRED)

    def test_volume_state_is_synchronized(self):
        order_item = self.trigger_volume_creation()
        instance = order_item.resource.scope

        instance.begin_creating()
        instance.save()

        instance.set_ok()
        instance.save()

        order_item.refresh_from_db()
        self.assertEqual(order_item.state, order_item.States.DONE)

    def trigger_volume_creation(self, **kwargs):
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        service_settings = fixture.openstack_tenant_service_settings

        image = openstack_tenant_factories.ImageFactory(
            settings=service_settings,
            min_disk=10240,
            min_ram=1024
        )

        attributes = {
            'image': openstack_tenant_factories.ImageFactory.get_url(image),
            'name': 'Volume',
            'size': 10 * 1024
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(type=VOLUME_TYPE, scope=service_settings)

        order_item = marketplace_factories.OrderItemFactory(offering=offering, attributes=attributes)
        order_item.order.approve()
        order_item.order.save()

        service = openstack_tenant_models.OpenStackTenantService.objects.create(
            customer=order_item.order.project.customer,
            settings=service_settings,
        )

        openstack_tenant_models.OpenStackTenantServiceProjectLink.objects.create(
            project=order_item.order.project,
            service=service,
        )

        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        order_item.refresh_from_db()
        return order_item


class VolumeDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()

        self.volume = self.fixture.volume
        self.volume.runtime_state = 'available'
        self.volume.save()

        self.offering = marketplace_factories.OfferingFactory(type=VOLUME_TYPE)
        self.resource = marketplace_factories.ResourceFactory(scope=self.volume, offering=self.offering)
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
        self.assertEqual(self.volume.state, openstack_tenant_models.Volume.States.DELETION_SCHEDULED)

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.volume.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(self.order_item.state, marketplace_models.OrderItem.States.DONE)
        self.assertEqual(self.resource.state, marketplace_models.Resource.States.TERMINATED)
        self.assertRaises(ObjectDoesNotExist, self.volume.refresh_from_db)

    def trigger_deletion(self):
        serialized_order = core_utils.serialize_instance(self.order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.volume.refresh_from_db()
