import uuid
from unittest import mock

from cinderclient import exceptions as cinder_exceptions
from ddt import data, ddt
from django.test import override_settings
from novaclient import exceptions as nova_exceptions

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.openstack.tests.unittests import test_backend
from waldur_openstack.openstack_tenant import models as openstack_tenant_models
from waldur_openstack.openstack_tenant import views as openstack_tenant_views
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)
from waldur_openstack.openstack_tenant.tests.helpers import (
    override_openstack_tenant_settings,
)

from .. import INSTANCE_TYPE
from .helpers import BaseOpenstackBackendTest
from .test_order_item import process_order


@ddt
class InstanceCreateTest(BaseOpenstackBackendTest):
    def setUp(self):
        super(InstanceCreateTest, self).setUp()
        self.service_settings.options = {'external_network_id': uuid.uuid4().hex}
        self.service_settings.save()
        self.image = openstack_tenant_factories.ImageFactory(
            settings=self.service_settings, min_disk=10240, min_ram=1024
        )
        self.flavor = openstack_tenant_factories.FlavorFactory(
            settings=self.service_settings
        )
        self.openstack_spl = self.openstack_tenant_fixture.spl
        self.project = self.openstack_tenant_fixture.project
        self.customer = self.openstack_tenant_fixture.customer

    def get_valid_data(self, **extra):
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        default = {
            'service_project_link': openstack_tenant_factories.OpenStackTenantServiceProjectLinkFactory.get_url(
                self.openstack_tenant_fixture.spl
            ),
            'flavor': openstack_tenant_factories.FlavorFactory.get_url(self.flavor),
            'image': openstack_tenant_factories.ImageFactory.get_url(self.image),
            'name': 'valid-name',
            'system_volume_size': self.image.min_disk,
            'internal_ips_set': [{'subnet': subnet_url}],
        }
        default.update(extra)
        return default

    def test_customer_quotas_updated_when_instance_is_created(self):
        order_item = self.trigger_instance_creation()
        instance = order_item.resource.scope

        self.assertEqual(
            self.openstack_tenant_fixture.customer.quotas.get(
                name='os_cpu_count'
            ).usage,
            instance.cores,
        )
        self.assertEqual(
            self.openstack_tenant_fixture.customer.quotas.get(name='os_ram_size').usage,
            instance.ram,
        )
        self.assertEqual(
            self.openstack_tenant_fixture.customer.quotas.get(
                name='os_storage_size'
            ).usage,
            instance.disk,
        )

    def test_availability_zone_should_be_available(self):
        zone = self.openstack_tenant_fixture.instance_availability_zone
        zone.available = False
        zone.save()
        payload = self.get_valid_data(
            availability_zone=openstack_tenant_factories.InstanceAvailabilityZoneFactory.get_url(
                zone
            )
        )

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.ERRED)

    @data('storage', 'ram', 'vcpu')
    def test_instance_cannot_be_created_if_service_project_link_quota_has_been_exceeded(
        self, quota
    ):
        payload = self.get_valid_data()
        self.openstack_spl.set_quota_limit(quota, 0)
        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.ERRED)

    @data('kt-experimental-ubuntu-18.04', 'vm_name')
    def test_not_create_instance_with_invalid_name(self, name):
        payload = self.get_valid_data()
        payload['name'] = name
        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_user_can_define_instance_availability_zone(self):
        zone = self.openstack_tenant_fixture.instance_availability_zone
        payload = self.get_valid_data(
            availability_zone=openstack_tenant_factories.InstanceAvailabilityZoneFactory.get_url(
                zone
            )
        )

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.EXECUTING)
        instance = order_item.resource.scope
        self.assertEqual(instance.availability_zone, zone)

    def test_user_can_define_instance_floating_ips(self):
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        floating_ip = self.openstack_tenant_fixture.floating_ip
        payload = self.get_valid_data(
            floating_ips=[
                {
                    'subnet': subnet_url,
                    'url': openstack_tenant_factories.FloatingIPFactory.get_url(
                        floating_ip
                    ),
                }
            ],
        )

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.EXECUTING)
        instance = order_item.resource.scope
        self.assertIn(floating_ip, instance.floating_ips)

    def test_user_cannot_allocate_floating_ip_if_quota_limit_is_reached(self):
        self.service_settings.quotas.filter(
            name=self.service_settings.Quotas.floating_ip_count
        ).update(limit=0)
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        self.openstack_tenant_fixture.floating_ip.status = 'ACTIVE'
        self.openstack_tenant_fixture.floating_ip.save()
        payload = self.get_valid_data(floating_ips=[{'subnet': subnet_url}],)

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_user_cannot_assign_floating_ip_from_other_settings_to_instance(self):
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        floating_ip = openstack_tenant_factories.FloatingIPFactory()
        payload = self.get_valid_data(
            floating_ips=[
                {
                    'subnet': subnet_url,
                    'url': openstack_tenant_factories.FloatingIPFactory.get_url(
                        floating_ip
                    ),
                }
            ],
        )

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_user_can_assign_active_floating_ip(self):
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        floating_ip = openstack_tenant_factories.FloatingIPFactory(
            settings=self.service_settings, runtime_state='ACTIVE'
        )
        payload = self.get_valid_data(
            floating_ips=[
                {
                    'subnet': subnet_url,
                    'url': openstack_tenant_factories.FloatingIPFactory.get_url(
                        floating_ip
                    ),
                }
            ],
        )

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.EXECUTING)

    @data('test', 'vm-name', 'vm', 'VM')
    def test_create_instance_with_valid_name(self, name):
        payload = self.get_valid_data()
        payload['name'] = name
        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.EXECUTING)

    def test_availability_zone_should_be_related_to_the_same_service_settings(self):
        zone = openstack_tenant_factories.InstanceAvailabilityZoneFactory()
        payload = self.get_valid_data(
            availability_zone=openstack_tenant_factories.InstanceAvailabilityZoneFactory.get_url(
                zone
            )
        )

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_cannot_create_instance_without_internal_ips(self):
        order_item = self.trigger_instance_creation(
            delete_ips=True, **self.get_valid_data()
        )

        self.assertEqual(order_item.state, order_item.States.ERRED)
        self.assertIn('internal_ips_set', order_item.error_message)

    def test_spl_quota_updated_by_signal_handler_when_instance_is_removed(self):
        order_item = self.trigger_instance_creation(**self.get_valid_data())
        instance = order_item.resource.scope
        instance.delete()

        self.assertEqual(
            self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.vcpu).usage, 0
        )
        self.assertEqual(
            self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.ram).usage, 0
        )
        self.assertEqual(
            self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.storage).usage,
            0,
        )

    def test_user_can_define_instance_subnets(self):
        subnet = self.openstack_tenant_fixture.subnet
        payload = self.get_valid_data(
            internal_ips_set=[
                {'subnet': openstack_tenant_factories.SubNetFactory.get_url(subnet)}
            ]
        )

        order_item = self.trigger_instance_creation(**payload)
        instance = order_item.resource.scope

        self.assertTrue(
            openstack_tenant_models.InternalIP.objects.filter(
                subnet=subnet, instance=instance
            ).exists()
        )

    def test_user_can_provision_instance(self):
        order_item = self.trigger_instance_creation(**self.get_valid_data())
        self.assertEqual(order_item.state, order_item.States.EXECUTING)

    def test_project_quotas_updated_when_instance_is_deleted(self):
        order_item = self.trigger_instance_creation(**self.get_valid_data())
        instance = order_item.resource.scope
        instance.delete()

        self.assertEqual(self.project.quotas.get(name='os_cpu_count').usage, 0)
        self.assertEqual(self.project.quotas.get(name='os_ram_size').usage, 0)
        self.assertEqual(self.project.quotas.get(name='os_storage_size').usage, 0)

    @data('instances')
    def test_quota_validation(self, quota_name):
        self.service_settings.quotas.filter(name=quota_name).update(limit=0)
        order_item = self.trigger_instance_creation(**self.get_valid_data())
        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_user_can_allocate_floating_ip(self):
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        self.openstack_tenant_fixture.floating_ip.status = 'ACTIVE'
        self.openstack_tenant_fixture.floating_ip.save()
        payload = self.get_valid_data(floating_ips=[{'subnet': subnet_url}],)

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.EXECUTING)
        instance = order_item.resource.scope
        self.assertEqual(instance.floating_ips.count(), 1)

    def test_service_settings_should_have_external_network_id(self):
        ss = self.openstack_spl.service.settings
        ss.options = {'external_network_id': 'invalid'}
        ss.save()

        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        payload = self.get_valid_data(floating_ips=[{'subnet': subnet_url}])

        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.ERRED)

    @override_openstack_tenant_settings(REQUIRE_AVAILABILITY_ZONE=True)
    def test_when_availability_zone_is_mandatory_and_does_not_exist_validation_succeeds(
        self,
    ):
        payload = self.get_valid_data()

        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.EXECUTING)

    def test_project_quotas_updated_when_instance_is_created(self):
        order_item = self.trigger_instance_creation(**self.get_valid_data())
        instance = order_item.resource.scope

        self.assertEqual(
            self.project.quotas.get(name='os_cpu_count').usage, instance.cores
        )
        self.assertEqual(
            self.project.quotas.get(name='os_ram_size').usage, instance.ram
        )
        self.assertEqual(
            self.project.quotas.get(name='os_storage_size').usage, instance.disk
        )

    def test_user_cannot_assign_floating_ip_to_disconnected_subnet(self):
        disconnected_subnet = openstack_tenant_factories.SubNetFactory(
            settings=self.openstack_tenant_fixture.openstack_tenant_service_settings
        )
        disconnected_subnet_url = openstack_tenant_factories.SubNetFactory.get_url(
            disconnected_subnet
        )
        floating_ip = self.openstack_tenant_fixture.floating_ip
        payload = self.get_valid_data(
            floating_ips=[
                {
                    'subnet': disconnected_subnet_url,
                    'url': openstack_tenant_factories.FloatingIPFactory.get_url(
                        floating_ip
                    ),
                }
            ],
        )

        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_user_cannot_assign_subnet_from_other_settings_to_instance(self):
        payload = self.get_valid_data(
            internal_ips_set=[
                {'subnet': openstack_tenant_factories.SubNetFactory.get_url()}
            ]
        )
        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_customer_quotas_updated_when_instance_is_deleted(self):
        order_item = self.trigger_instance_creation(**self.get_valid_data())
        instance = order_item.resource.scope
        instance.delete()

        self.assertEqual(self.customer.quotas.get(name='os_cpu_count').usage, 0)
        self.assertEqual(self.customer.quotas.get(name='os_ram_size').usage, 0)
        self.assertEqual(self.customer.quotas.get(name='os_storage_size').usage, 0)

    def test_quotas_update(self):
        order_item = self.trigger_instance_creation(**self.get_valid_data())
        self.assertEqual(order_item.state, order_item.States.EXECUTING)

        instance = order_item.resource.scope
        Quotas = self.service_settings.Quotas
        self.assertEqual(
            self.service_settings.quotas.get(name=Quotas.ram).usage, instance.ram
        )
        self.assertEqual(
            self.service_settings.quotas.get(name=Quotas.storage).usage, instance.disk
        )
        self.assertEqual(
            self.service_settings.quotas.get(name=Quotas.vcpu).usage, instance.cores
        )
        self.assertEqual(
            self.service_settings.quotas.get(name=Quotas.instances).usage, 1
        )

        self.assertEqual(
            self.openstack_spl.quotas.get(name=Quotas.ram).usage, instance.ram
        )
        self.assertEqual(
            self.openstack_spl.quotas.get(name=Quotas.storage).usage, instance.disk
        )
        self.assertEqual(
            self.openstack_spl.quotas.get(name=Quotas.vcpu).usage, instance.cores
        )

        self.assertEqual(
            self.service_settings.scope.quotas.get(name=Quotas.ram).usage, instance.ram,
        )
        self.assertEqual(
            self.service_settings.scope.quotas.get(name=Quotas.storage).usage,
            instance.disk,
        )
        self.assertEqual(
            self.service_settings.scope.quotas.get(name=Quotas.vcpu).usage,
            instance.cores,
        )
        self.assertEqual(
            self.service_settings.scope.quotas.get(name=Quotas.instances).usage, 1
        )

    def test_user_cannot_use_floating_ip_assigned_to_other_instance(self):
        subnet_url = openstack_tenant_factories.SubNetFactory.get_url(self.subnet)
        internal_ip = openstack_tenant_factories.InternalIPFactory(subnet=self.subnet)
        floating_ip = openstack_tenant_factories.FloatingIPFactory(
            settings=self.service_settings,
            runtime_state='ACTIVE',
            internal_ip=internal_ip,
        )
        payload = self.get_valid_data(
            floating_ips=[
                {
                    'subnet': subnet_url,
                    'url': openstack_tenant_factories.FloatingIPFactory.get_url(
                        floating_ip
                    ),
                }
            ],
        )

        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.ERRED)
        self.assertIn('floating_ips', order_item.error_message)

    def test_volume_AZ_should_be_matched_with_instance_AZ(self):
        # Arrange
        vm_az = self.openstack_tenant_fixture.instance_availability_zone
        volume_az = self.openstack_tenant_fixture.volume_availability_zone

        private_ss = self.openstack_tenant_fixture.openstack_tenant_service_settings
        shared_ss = private_ss.scope.service_settings

        shared_ss.options = {'valid_availability_zones': {vm_az.name: volume_az.name}}
        shared_ss.save()

        vm_az_url = openstack_tenant_factories.InstanceAvailabilityZoneFactory.get_url(
            vm_az
        )
        payload = self.get_valid_data(availability_zone=vm_az_url)

        # Act
        order_item = self.trigger_instance_creation(**payload)

        # Assert
        self.assertEqual(order_item.state, order_item.States.EXECUTING)
        instance = order_item.resource.scope

        self.assertEqual(instance.availability_zone, vm_az)
        self.assertEqual(instance.volumes.first().availability_zone, volume_az)
        self.assertEqual(instance.volumes.last().availability_zone, volume_az)

    @override_openstack_tenant_settings(REQUIRE_AVAILABILITY_ZONE=True)
    def test_when_availability_zone_is_mandatory_and_exists_validation_fails(self):
        self.openstack_tenant_fixture.instance_availability_zone
        payload = self.get_valid_data()

        order_item = self.trigger_instance_creation(**payload)

        self.assertEqual(order_item.state, order_item.States.ERRED)

    def test_security_groups_is_not_required(self):
        payload = self.get_valid_data()
        self.assertNotIn('security_groups', payload)
        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.EXECUTING)

    def test_add_instance_with_security_groups(self):
        security_groups = openstack_tenant_factories.SecurityGroupFactory.create_batch(
            2, settings=self.service_settings
        )
        payload = self.get_valid_data()
        payload['security_groups'] = [
            {'url': openstack_tenant_factories.SecurityGroupFactory.get_url(sg)}
            for sg in security_groups
        ]

        order_item = self.trigger_instance_creation(**payload)
        self.assertEqual(order_item.state, order_item.States.EXECUTING)

        reread_instance = order_item.resource.scope
        reread_security_groups = list(reread_instance.security_groups.all())
        self.assertEquals(reread_security_groups, security_groups)


class InstanceDeleteTest(test_backend.BaseBackendTestCase):
    def setUp(self):
        super(InstanceDeleteTest, self).setUp()
        # self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.instance = openstack_tenant_factories.InstanceFactory(
            state=openstack_tenant_models.Instance.States.OK,
            runtime_state=openstack_tenant_models.Instance.RuntimeStates.SHUTOFF,
            backend_id='VALID_ID',
        )
        self.instance.increase_backend_quotas_usage()
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
        self.mocked_nova().servers.get.side_effect = nova_exceptions.NotFound(code=404)
        openstack_tenant_views.InstanceViewSet.async_executor = False

    def tearDown(self):
        super(InstanceDeleteTest, self).tearDown()
        openstack_tenant_views.InstanceViewSet.async_executor = True

    def assert_quota_usage(self, quotas, name, value):
        self.assertEqual(quotas.get(name=name).usage, value)

    def mock_volumes(self, delete_data_volume=True):
        self.data_volume = self.instance.volumes.get(bootable=False)
        self.data_volume.backend_id = 'DATA_VOLUME_ID'
        self.data_volume.state = openstack_tenant_models.Volume.States.OK
        self.data_volume.save()
        self.data_volume.increase_backend_quotas_usage()

        self.system_volume = self.instance.volumes.get(bootable=True)
        self.system_volume.backend_id = 'SYSTEM_VOLUME_ID'
        self.system_volume.state = openstack_tenant_models.Volume.States.OK
        self.system_volume.save()
        self.system_volume.increase_backend_quotas_usage()

        def get_volume(backend_id):
            if not delete_data_volume and backend_id == self.data_volume.backend_id:
                mocked_volume = mock.Mock()
                mocked_volume.status = 'available'
                return mocked_volume
            raise cinder_exceptions.NotFound(code=404)

        self.mocked_cinder().volumes.get.side_effect = get_volume

    def trigger_deletion(self, attributes=None):
        with override_settings(
            CELERY_ALWAYS_EAGER=True, CELERY_EAGER_PROPAGATES_EXCEPTIONS=True
        ):
            if attributes:
                self.order_item.attributes = attributes
                self.order_item.save()
            process_order(self.order_item.order, self.fixture.staff)

            self.order_item.refresh_from_db()
            self.resource.refresh_from_db()

    def test_nova_methods_are_called_if_instance_is_deleted_with_volumes(self):
        self.mock_volumes(True)
        self.trigger_deletion()

        nova = self.mocked_nova()
        nova.servers.delete.assert_called_once_with(self.instance.backend_id)
        nova.servers.get.assert_called_once_with(self.instance.backend_id)

    def test_database_models_deleted(self):
        self.mock_volumes(True)
        self.trigger_deletion()

        self.assertFalse(
            openstack_tenant_models.Instance.objects.filter(
                id=self.instance.id
            ).exists()
        )
        for volume in self.instance.volumes.all():
            self.assertFalse(
                openstack_tenant_models.Volume.objects.filter(id=volume.id).exists()
            )

    def test_quotas_updated_if_instance_is_deleted_with_volumes(self):
        self.mock_volumes(True)
        self.trigger_deletion()

        self.instance.service_project_link.service.settings.refresh_from_db()
        quotas = self.instance.service_project_link.service.settings.quotas
        tenant_quotas = self.instance.service_project_link.service.settings.scope.quotas

        for scope in (quotas, tenant_quotas):
            self.assert_quota_usage(scope, 'instances', 0)
            self.assert_quota_usage(scope, 'vcpu', 0)
            self.assert_quota_usage(scope, 'ram', 0)

            self.assert_quota_usage(scope, 'volumes', 0)
            self.assert_quota_usage(scope, 'storage', 0)

    def test_backend_methods_are_called_if_instance_is_deleted_without_volumes(self):
        self.mock_volumes(False)
        self.trigger_deletion({'delete_volumes': False})

        nova = self.mocked_nova()
        nova.volumes.delete_server_volume.assert_called_once_with(
            self.instance.backend_id, self.data_volume.backend_id
        )

        nova.servers.delete.assert_called_once_with(self.instance.backend_id)
        nova.servers.get.assert_called_once_with(self.instance.backend_id)

    def test_system_volume_is_deleted_but_data_volume_exists(self):
        self.mock_volumes(False)
        self.trigger_deletion({'delete_volumes': False})

        self.assertFalse(
            openstack_tenant_models.Instance.objects.filter(
                id=self.instance.id
            ).exists()
        )
        self.assertTrue(
            openstack_tenant_models.Volume.objects.filter(
                id=self.data_volume.id
            ).exists()
        )
        self.assertFalse(
            openstack_tenant_models.Volume.objects.filter(
                id=self.system_volume.id
            ).exists()
        )

    def test_quotas_updated_if_instance_is_deleted_without_volumes(self):
        self.mock_volumes(False)
        self.trigger_deletion({'delete_volumes': False})

        settings = self.instance.service_project_link.service.settings
        settings.refresh_from_db()

        self.assert_quota_usage(settings.quotas, 'instances', 0)
        self.assert_quota_usage(settings.quotas, 'vcpu', 0)
        self.assert_quota_usage(settings.quotas, 'ram', 0)

        self.assert_quota_usage(settings.quotas, 'volumes', 1)
        self.assert_quota_usage(settings.quotas, 'storage', self.data_volume.size)

    def test_instance_cannot_be_deleted_if_it_has_backups(self):
        self.instance = openstack_tenant_factories.InstanceFactory(
            state=openstack_tenant_models.Instance.States.OK,
            runtime_state=openstack_tenant_models.Instance.RuntimeStates.SHUTOFF,
            backend_id='VALID_ID',
        )
        openstack_tenant_factories.BackupFactory(
            instance=self.instance, state=openstack_tenant_models.Backup.States.OK
        )

        self.trigger_deletion()

        self.assertEqual(self.order_item.state, self.order_item.States.ERRED)

    def test_neutron_methods_are_called_if_instance_is_deleted_with_floating_ips(self):
        self.mock_volumes(False)
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        internal_ip = openstack_tenant_factories.InternalIPFactory.create(
            instance=self.instance, subnet=fixture.subnet
        )
        settings = self.instance.service_project_link.service.settings
        floating_ip = openstack_tenant_factories.FloatingIPFactory.create(
            internal_ip=internal_ip, settings=settings
        )
        self.trigger_deletion(
            {'release_floating_ips': True, 'delete_volumes': False,}
        )
        self.mocked_neutron().delete_floatingip.assert_called_once_with(
            floating_ip.backend_id
        )

    def test_neutron_methods_are_not_called_if_instance_does_not_have_any_floating_ips_yet(
        self,
    ):
        self.mock_volumes(False)
        self.trigger_deletion(
            {'release_floating_ips': True, 'delete_volumes': False,}
        )
        self.assertEqual(self.mocked_neutron().delete_floatingip.call_count, 0)

    def test_neutron_methods_are_not_called_if_user_did_not_ask_for_floating_ip_removal_explicitly(
        self,
    ):
        self.mock_volumes(False)
        self.mocked_neutron().show_floatingip.return_value = {
            'floatingip': {'status': 'DOWN'}
        }
        fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        internal_ip = openstack_tenant_factories.InternalIPFactory.create(
            instance=self.instance, subnet=fixture.subnet
        )
        settings = self.instance.service_project_link.service.settings
        openstack_tenant_factories.FloatingIPFactory.create(
            internal_ip=internal_ip, settings=settings
        )
        self.trigger_deletion({'release_floating_ips': False, 'delete_volumes': False})
        self.assertEqual(self.mocked_neutron().delete_floatingip.call_count, 0)
