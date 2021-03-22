from unittest import mock

from ddt import data, ddt
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import create_offering_components
from waldur_mastermind.marketplace_openstack.tests import fixtures as package_fixtures
from waldur_mastermind.marketplace_openstack.tests.utils import BaseOpenStackTest
from waldur_openstack.openstack import models as openstack_models
from waldur_openstack.openstack.tests import factories as openstack_factories
from waldur_openstack.openstack.tests.helpers import override_openstack_settings
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)

from .. import (
    CORES_TYPE,
    INSTANCE_TYPE,
    RAM_TYPE,
    STORAGE_TYPE,
    TENANT_TYPE,
    VOLUME_TYPE,
)


def process_order(order, user):
    serialized_order = core_utils.serialize_instance(order)
    serialized_user = core_utils.serialize_instance(user)
    marketplace_tasks.process_order(serialized_order, serialized_user)


class TenantGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = package_fixtures.MarketplaceOpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(type=TENANT_TYPE)
        self.order = marketplace_factories.OrderFactory(project=self.fixture.project)
        self.order_item = marketplace_factories.OrderItemFactory(
            order=self.order,
            offering=self.offering,
            attributes=dict(user_username='admin', user_userpassword='secret'),
        )

    def get_order_item(self):
        self.client.force_login(self.fixture.manager)
        url = marketplace_factories.OrderItemFactory.get_url(self.order_item)
        return self.client.get(url)

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=True)
    def test_secret_attributes_are_rendered(self):
        response = self.get_order_item()
        self.assertTrue('user_username' in response.data['attributes'])

    @override_openstack_settings(TENANT_CREDENTIALS_VISIBLE=False)
    def test_secret_attributes_are_not_rendered(self):
        response = self.get_order_item()
        self.assertFalse('user_username' in response.data['attributes'])


@ddt
class TenantCreateTest(BaseOpenStackTest):
    def setUp(self):
        super(TenantCreateTest, self).setUp()
        self.fixture = package_fixtures.MarketplaceOpenStackFixture()
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.fixture.openstack_service_settings,
            type=TENANT_TYPE,
            state=marketplace_models.Offering.States.ACTIVE,
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)

    @data('staff', 'owner', 'manager', 'admin')
    def test_when_order_is_created_items_are_validated(self, user):
        response = self.create_order(user=user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_mandatory_attributes_are_checked(self):
        response = self.create_order(dict(user_username=None))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('user_username' in response.data)

    def test_limits_are_not_checked_if_offering_components_limits_are_not_defined(self):
        response = self.create_order(
            limits={'cores': 2, 'ram': 1024 * 10, 'storage': 1024 * 1024 * 10}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_limits_are_checked_against_offering_components(self):
        create_offering_components(self.offering)
        self.offering.components.filter(type=CORES_TYPE).update(max_value=10)
        self.offering.components.filter(type=RAM_TYPE).update(max_value=1024 * 10)
        self.offering.components.filter(type=STORAGE_TYPE).update(
            max_value=1024 * 1024 * 10
        )

        response = self.create_order(
            limits={'cores': 20, 'ram': 1024 * 100, 'storage': 1024 * 1024 * 100}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def create_plan_component(self, type, price):
        return marketplace_factories.PlanComponentFactory(
            component=self.offering.components.get(type=type),
            plan=self.plan,
            price=price,
        )

    def test_cost_estimate_is_calculated_using_limits(self):
        create_offering_components(self.offering)

        self.create_plan_component(CORES_TYPE, 1)
        self.create_plan_component(RAM_TYPE, 0.5)
        self.create_plan_component(STORAGE_TYPE, 0.1)

        response = self.create_order(
            limits={'cores': 20, 'ram': 1024 * 100, 'storage': 1024 * 10000}
        )
        expected = 20 * 1 + 100 * 0.5 + 10000 * 0.1
        self.assertEqual(float(response.data['total_cost']), expected)

    def test_cost_estimate_is_calculated_using_dynamic_storage(self):
        create_offering_components(self.offering)

        self.create_plan_component(CORES_TYPE, 1)
        self.create_plan_component(RAM_TYPE, 0.5)
        marketplace_models.OfferingComponent.objects.create(
            offering=self.offering,
            type='gigabytes_llvm',
            billing_type=marketplace_models.OfferingComponent.BillingTypes.FIXED,
        )
        self.create_plan_component('gigabytes_llvm', 0.1)

        response = self.create_order(
            limits={'cores': 20, 'ram': 1024 * 100, 'gigabytes_llvm': 10000}
        )

        expected = 20 * 1 + 100 * 0.5 + 10000 * 0.1
        self.assertEqual(float(response.data['total_cost']), expected)

    def create_order(self, add_attributes=None, user='staff', limits=None):
        project_url = structure_factories.ProjectFactory.get_url(self.fixture.project)
        offering_url = marketplace_factories.OfferingFactory.get_url(self.offering)
        plan_url = marketplace_factories.PlanFactory.get_url(self.plan)

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
                {'offering': offering_url, 'plan': plan_url, 'attributes': attributes,},
            ],
        }
        if limits:
            payload['items'][0]['limits'] = limits

        self.client.force_login(getattr(self.fixture, user))
        url = marketplace_factories.OrderFactory.get_list_url()
        return self.client.post(url, payload)

    def test_when_order_item_is_approved_openstack_tenant_is_created(self):
        # Arrange
        order = marketplace_factories.OrderFactory(
            state=marketplace_models.Order.States.REQUESTED_FOR_APPROVAL,
            project=self.fixture.project,
        )
        attributes = dict(
            name='My first VPC',
            description='Database cluster',
            user_username='admin_user',
        )
        order_item = marketplace_factories.OrderItemFactory(
            order=order, offering=self.offering, attributes=attributes, plan=self.plan
        )

        process_order(order, self.fixture.staff)

        # Assert
        order_item.refresh_from_db()
        self.assertTrue(isinstance(order_item.resource.scope, openstack_models.Tenant))

    def test_order_item_set_state_done(self):
        tenant = openstack_factories.TenantFactory()
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
        self.assertEqual(
            order_item.resource.state, marketplace_models.Resource.States.OK
        )

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)

    def test_volume_type_limits_are_propagated(self):
        create_offering_components(self.offering)

        marketplace_models.OfferingComponent.objects.create(
            offering=self.offering,
            type='gigabytes_llvm',
            billing_type=marketplace_models.OfferingComponent.BillingTypes.FIXED,
        )

        response = self.create_order(
            limits={'cores': 2, 'ram': 1024 * 10, 'gigabytes_llvm': 10}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = marketplace_models.Order.objects.get(uuid=response.data['uuid'])
        process_order(order, self.fixture.staff)

        tenant = order.items.get().resource.scope
        self.assertEqual(tenant.quotas.get(name='gigabytes_llvm').limit, 10)

    def test_volume_type_limits_are_initialized_with_zero_by_default(self):
        create_offering_components(self.offering)

        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name='llvm')
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name='ssd')
        openstack_factories.VolumeTypeFactory(settings=self.offering.scope, name='rbd')

        response = self.create_order(
            limits={'cores': 2, 'ram': 1024 * 10, 'gigabytes_llvm': 10}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        order = marketplace_models.Order.objects.get(uuid=response.data['uuid'])
        process_order(order, self.fixture.staff)

        tenant = order.items.get().resource.scope
        self.assertEqual(tenant.quotas.get(name='gigabytes_llvm').limit, 10)
        self.assertEqual(tenant.quotas.get(name='gigabytes_ssd').limit, 0)
        self.assertEqual(tenant.quotas.get(name='gigabytes_rbd').limit, 0)


class TenantMutateTest(test.APITransactionTestCase):
    def setUp(self):
        super(TenantMutateTest, self).setUp()
        self.fixture = package_fixtures.MarketplaceOpenStackFixture()
        self.tenant = self.fixture.openstack_tenant
        self.offering = marketplace_factories.OfferingFactory(type=TENANT_TYPE)
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
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
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATING
        )
        self.assertEqual(
            self.tenant.state, openstack_models.Tenant.States.DELETION_SCHEDULED
        )

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.tenant.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.DONE
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATED
        )
        self.assertRaises(ObjectDoesNotExist, self.tenant.refresh_from_db)

    def trigger_deletion(self):
        process_order(self.order_item.order, self.fixture.staff)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.tenant.refresh_from_db()


class InstanceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.service_settings = self.fixture.openstack_tenant_service_settings

    def test_instance_is_created_when_order_item_is_processed(self):
        order_item = self.trigger_instance_creation()
        self.assertEqual(
            order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertTrue(
            openstack_tenant_models.Instance.objects.filter(
                name='virtual-machine'
            ).exists()
        )

    def test_availability_zone_is_passed_to_plugin(self):
        availability_zone = openstack_tenant_factories.InstanceAvailabilityZoneFactory(
            settings=self.fixture.openstack_tenant_service_settings
        )
        az_url = openstack_tenant_factories.InstanceAvailabilityZoneFactory.get_url(
            availability_zone
        )
        order_item = self.trigger_instance_creation(availability_zone=az_url)
        self.assertEqual(order_item.resource.scope.availability_zone, availability_zone)

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
        self.assertEqual(
            order_item.resource.state, marketplace_models.Resource.States.OK
        )

        order_item.order.refresh_from_db()
        self.assertEqual(order_item.order.state, marketplace_models.Order.States.DONE)

    def test_create_resource_of_volume_if_instance_created(self):
        order_item = self.trigger_instance_creation()
        instance = order_item.resource.scope
        volume = instance.volumes.first()
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=volume).exists()
        )

    def trigger_instance_creation(self, **kwargs):
        image = openstack_tenant_factories.ImageFactory(
            settings=self.service_settings, min_disk=10240, min_ram=1024
        )
        flavor = openstack_tenant_factories.FlavorFactory(
            settings=self.service_settings
        )

        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(
            self.fixture.subnet
        )
        attributes = {
            'flavor': openstack_tenant_factories.FlavorFactory.get_url(flavor),
            'image': openstack_tenant_factories.ImageFactory.get_url(image),
            'name': 'virtual-machine',
            'system_volume_size': image.min_disk,
            'internal_ips_set': [{'subnet': subnet_url}],
            'ssh_public_key': structure_factories.SshPublicKeyFactory.get_url(
                structure_factories.SshPublicKeyFactory(user=self.fixture.manager)
            ),
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=INSTANCE_TYPE, scope=self.service_settings
        )
        marketplace_factories.OfferingFactory(
            type=VOLUME_TYPE, scope=self.service_settings
        )
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            state=marketplace_models.Order.States.EXECUTING,
        )
        order_item = marketplace_factories.OrderItemFactory(
            offering=offering, attributes=attributes, order=order,
        )

        process_order(order_item.order, self.fixture.owner)

        order_item.refresh_from_db()
        return order_item


class InstanceDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.instance = self.fixture.instance
        self.offering = marketplace_factories.OfferingFactory(type=INSTANCE_TYPE)
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.instance, offering=self.offering
        )
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
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATING
        )
        self.assertEqual(
            self.instance.state,
            openstack_tenant_models.Instance.States.DELETION_SCHEDULED,
        )

    @mock.patch('waldur_openstack.openstack_tenant.views.executors')
    def test_cancel_of_volume_deleting(self, mock_executors):
        self.order_item.attributes = {'delete_volumes': False}
        self.order_item.save()
        self.trigger_deletion()
        self.assertFalse(
            mock_executors.InstanceDeleteExecutor.execute.call_args[1]['delete_volumes']
        )

    @mock.patch('waldur_openstack.openstack_tenant.views.executors')
    def test_cancel_of_floating_ips_deleting(self, mock_executors):
        self.order_item.attributes = {'release_floating_ips': False}
        self.order_item.save()
        self.trigger_deletion()
        self.assertFalse(
            mock_executors.InstanceDeleteExecutor.execute.call_args[1][
                'release_floating_ips'
            ]
        )

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.instance.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.DONE
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATED
        )
        self.assertRaises(ObjectDoesNotExist, self.instance.refresh_from_db)

    def trigger_deletion(self):
        process_order(self.order_item.order, self.fixture.staff)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.instance.refresh_from_db()


class VolumeCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.service_settings = self.fixture.openstack_tenant_service_settings

    def test_volume_is_created_when_order_item_is_processed(self):
        order_item = self.trigger_volume_creation()
        self.assertEqual(
            order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertTrue(
            openstack_tenant_models.Volume.objects.filter(name='Volume').exists()
        )

    def test_availability_zone_is_passed_to_plugin(self):
        availability_zone = openstack_tenant_factories.VolumeAvailabilityZoneFactory(
            settings=self.fixture.openstack_tenant_service_settings
        )
        az_url = openstack_tenant_factories.VolumeAvailabilityZoneFactory.get_url(
            availability_zone
        )
        order_item = self.trigger_volume_creation(availability_zone=az_url)
        self.assertEqual(order_item.resource.scope.availability_zone, availability_zone)

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
        image = openstack_tenant_factories.ImageFactory(
            settings=self.service_settings, min_disk=10240, min_ram=1024
        )

        attributes = {
            'image': openstack_tenant_factories.ImageFactory.get_url(image),
            'name': 'Volume',
            'size': 10 * 1024,
        }
        attributes.update(kwargs)

        offering = marketplace_factories.OfferingFactory(
            type=VOLUME_TYPE, scope=self.service_settings
        )

        order_item = marketplace_factories.OrderItemFactory(
            offering=offering,
            attributes=attributes,
            order__project=self.fixture.project,
        )
        order_item.order.approve()
        order_item.order.save()

        process_order(order_item.order, self.fixture.staff)

        order_item.refresh_from_db()
        return order_item


class VolumeDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()

        self.volume = self.fixture.volume
        self.volume.runtime_state = 'available'
        self.volume.save()

        self.offering = marketplace_factories.OfferingFactory(type=VOLUME_TYPE)
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.volume, offering=self.offering
        )
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
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.EXECUTING
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATING
        )
        self.assertEqual(
            self.volume.state, openstack_tenant_models.Volume.States.DELETION_SCHEDULED
        )

    def test_deletion_is_completed(self):
        self.trigger_deletion()
        self.volume.delete()

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()

        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.DONE
        )
        self.assertEqual(
            self.resource.state, marketplace_models.Resource.States.TERMINATED
        )
        self.assertRaises(ObjectDoesNotExist, self.volume.refresh_from_db)

    def trigger_deletion(self):
        process_order(self.order_item.order, self.fixture.staff)

        self.order_item.refresh_from_db()
        self.resource.refresh_from_db()
        self.volume.refresh_from_db()


class TenantUpdateLimitTestBase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.offering = marketplace_factories.OfferingFactory(type=TENANT_TYPE)
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            plan=self.plan,
            state=marketplace_models.Resource.States.OK,
        )
        tenant = self.fixture.tenant
        self.mock_get_backend = mock.MagicMock()
        tenant.get_backend = self.mock_get_backend
        self.resource.scope = tenant
        self.resource.save()
        self.quotas = {
            'network_count': 100,
            'cores': 4,
            'ram': 1024,
            'storage': 1024,
            'snapshots': 50,
            'instances': 30,
            'floating_ip_count': 50,
            'subnet_count': 100,
            'volumes': 50,
            'security_group_rule_count': 100,
            'security_group_count': 100,
        }


class TenantUpdateLimitTest(TenantUpdateLimitTestBase):
    def setUp(self):
        super(TenantUpdateLimitTest, self).setUp()
        self.order_item = marketplace_factories.OrderItemFactory(
            type=marketplace_models.OrderItem.Types.UPDATE,
            resource=self.resource,
            plan=self.resource.plan,
            offering=self.offering,
            limits=self.quotas,
            attributes={'old_limits': self.resource.limits},
        )

    def test_resource_limits_have_been_updated_if_backend_does_not_raise_exception(
        self,
    ):
        marketplace_utils.process_order_item(self.order_item, self.fixture.staff)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits, self.quotas)
        self.order_item.refresh_from_db()
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.DONE
        )

    def test_resource_limits_have_been_not_updated_if_backend_raises_exception(self):
        self.mock_get_backend().push_tenant_quotas = mock.Mock(
            side_effect=Exception('foo')
        )
        marketplace_utils.process_order_item(self.order_item, self.fixture.staff)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits, {})
        self.order_item.refresh_from_db()
        self.assertEqual(
            self.order_item.state, marketplace_models.OrderItem.States.ERRED
        )
        self.assertEqual(self.order_item.error_message, 'foo')

    def test_volume_type_quotas_are_propagated(self):
        del self.quotas['storage']
        self.quotas['gigabytes_lvmdriver-1'] = 10
        self.quotas['gigabytes_backup'] = 30
        marketplace_utils.process_order_item(self.order_item, self.fixture.staff)
        _, quotas = self.mock_get_backend().push_tenant_quotas.call_args[0]
        self.assertTrue('gigabytes_lvmdriver-1' in quotas)
        self.assertEqual(quotas['storage'], 40 * 1024)


class TenantUpdateLimitValidationTest(TenantUpdateLimitTestBase):
    def setUp(self):
        super(TenantUpdateLimitValidationTest, self).setUp()
        marketplace_models.OfferingComponent.objects.create(
            offering=self.offering, max_value=20, min_value=2, type='cores'
        )

    def update_limits(self, user, resource, limits=None):
        limits = limits or {'cores': 10}
        self.client.force_authenticate(user)
        url = marketplace_factories.ResourceFactory.get_url(resource, 'update_limits')
        payload = {'limits': limits}
        return self.client.post(url, payload)

    def test_validation_if_requested_available_limits(self):
        response = self.update_limits(self.fixture.staff, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_validation_if_requested_unavailable_limits(self):
        response = self.update_limits(self.fixture.staff, self.resource, {'foo': 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validation_if_value_limit_in_confines(self):
        response = self.update_limits(self.fixture.staff, self.resource)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_validation_if_value_limit_more_max(self):
        response = self.update_limits(self.fixture.staff, self.resource, {'cores': 30})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validation_if_value_limit_less_min(self):
        response = self.update_limits(self.fixture.staff, self.resource, {'cores': 1})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
