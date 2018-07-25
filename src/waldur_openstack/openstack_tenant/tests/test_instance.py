import uuid

from cinderclient import exceptions as cinder_exceptions
from ddt import ddt, data
from django.conf import settings
from django.test import override_settings
from novaclient import exceptions as nova_exceptions
from rest_framework import status, test
import mock
from six.moves import urllib

from waldur_openstack.openstack.tests.unittests import test_backend
from waldur_core.structure.tests import factories as structure_factories

from . import factories, fixtures
from .. import models, views


@ddt
class InstanceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.openstack_tenant_fixture = fixtures.OpenStackTenantFixture()
        self.openstack_settings = self.openstack_tenant_fixture.openstack_tenant_service_settings
        self.openstack_settings.options = {'external_network_id': uuid.uuid4().hex}
        self.openstack_settings.save()
        self.openstack_spl = self.openstack_tenant_fixture.spl
        self.project = self.openstack_tenant_fixture.project
        self.customer = self.openstack_tenant_fixture.customer
        self.image = factories.ImageFactory(settings=self.openstack_settings, min_disk=10240, min_ram=1024)
        self.flavor = factories.FlavorFactory(settings=self.openstack_settings)
        self.subnet = self.openstack_tenant_fixture.subnet

        self.client.force_authenticate(user=self.openstack_tenant_fixture.owner)
        self.url = factories.InstanceFactory.get_list_url()

    def get_valid_data(self, **extra):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        default = {
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.openstack_spl),
            'flavor': factories.FlavorFactory.get_url(self.flavor),
            'image': factories.ImageFactory.get_url(self.image),
            'name': 'Valid name',
            'system_volume_size': self.image.min_disk,
            'internal_ips_set': [{'subnet': subnet_url}],
        }
        default.update(extra)
        return default

    def test_quotas_update(self):
        response = self.client.post(self.url, self.get_valid_data())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(uuid=response.data['uuid'])
        Quotas = self.openstack_settings.Quotas
        self.assertEqual(self.openstack_settings.quotas.get(name=Quotas.ram).usage, instance.ram)
        self.assertEqual(self.openstack_settings.quotas.get(name=Quotas.storage).usage, instance.disk)
        self.assertEqual(self.openstack_settings.quotas.get(name=Quotas.vcpu).usage, instance.cores)
        self.assertEqual(self.openstack_settings.quotas.get(name=Quotas.instances).usage, 1)

        self.assertEqual(self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.ram).usage, instance.ram)
        self.assertEqual(self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.storage).usage, instance.disk)
        self.assertEqual(self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.vcpu).usage, instance.cores)

    def test_project_quotas_updated_when_instance_is_created(self):
        response = self.client.post(self.url, self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data['uuid'])

        self.assertEqual(self.project.quotas.get(name='os_cpu_count').usage, instance.cores)
        self.assertEqual(self.project.quotas.get(name='os_ram_size').usage, instance.ram)
        self.assertEqual(self.project.quotas.get(name='os_storage_size').usage, instance.disk)

    def test_customer_quotas_updated_when_instance_is_created(self):
        response = self.client.post(self.url, self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data['uuid'])

        self.assertEqual(self.customer.quotas.get(name='os_cpu_count').usage, instance.cores)
        self.assertEqual(self.customer.quotas.get(name='os_ram_size').usage, instance.ram)
        self.assertEqual(self.customer.quotas.get(name='os_storage_size').usage, instance.disk)

    def test_spl_quota_updated_by_signal_handler_when_instance_is_removed(self):
        response = self.client.post(self.url, self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data['uuid'])
        instance.delete()

        self.assertEqual(self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.vcpu).usage, 0)
        self.assertEqual(self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.ram).usage, 0)
        self.assertEqual(self.openstack_spl.quotas.get(name=self.openstack_spl.Quotas.storage).usage, 0)

    def test_project_quotas_updated_when_instance_is_deleted(self):
        response = self.client.post(self.url, self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data['uuid'])
        instance.delete()

        self.assertEqual(self.project.quotas.get(name='os_cpu_count').usage, 0)
        self.assertEqual(self.project.quotas.get(name='os_ram_size').usage, 0)
        self.assertEqual(self.project.quotas.get(name='os_storage_size').usage, 0)

    def test_customer_quotas_updated_when_instance_is_deleted(self):
        response = self.client.post(self.url, self.get_valid_data())
        instance = models.Instance.objects.get(uuid=response.data['uuid'])
        instance.delete()

        self.assertEqual(self.customer.quotas.get(name='os_cpu_count').usage, 0)
        self.assertEqual(self.customer.quotas.get(name='os_ram_size').usage, 0)
        self.assertEqual(self.customer.quotas.get(name='os_storage_size').usage, 0)

    @data('storage', 'ram', 'vcpu')
    def test_instance_cannot_be_created_if_service_project_link_quota_has_been_exceeded(self, quota):
        payload = self.get_valid_data()
        self.openstack_spl.set_quota_limit(quota, 0)
        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data('instances')
    def test_quota_validation(self, quota_name):
        self.openstack_settings.quotas.filter(name=quota_name).update(limit=0)
        response = self.client.post(self.url, self.get_valid_data())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_provision_instance(self):
        response = self.client.post(self.url, self.get_valid_data())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_user_can_define_instance_subnets(self):
        subnet = self.openstack_tenant_fixture.subnet
        data = self.get_valid_data(internal_ips_set=[{'subnet': factories.SubNetFactory.get_url(subnet)}])

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(uuid=response.data['uuid'])
        self.assertTrue(models.InternalIP.objects.filter(subnet=subnet, instance=instance).exists())

    def test_user_cannot_assign_subnet_from_other_settings_to_instance(self):
        data = self.get_valid_data(internal_ips_set=[{'subnet': factories.SubNetFactory.get_url()}])
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_define_instance_floating_ips(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        floating_ip = self.openstack_tenant_fixture.floating_ip
        data = self.get_valid_data(
            floating_ips=[{'subnet': subnet_url, 'url': factories.FloatingIPFactory.get_url(floating_ip)}],
        )

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        instance = models.Instance.objects.get(uuid=response.data['uuid'])
        self.assertIn(floating_ip, instance.floating_ips)

    def test_user_cannot_assign_floating_ip_from_other_settings_to_instance(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        floating_ip = factories.FloatingIPFactory()
        data = self.get_valid_data(
            floating_ips=[{'subnet': subnet_url, 'url': factories.FloatingIPFactory.get_url(floating_ip)}],
        )

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_assign_floating_ip_to_disconnected_subnet(self):
        disconnected_subnet = factories.SubNetFactory(
            settings=self.openstack_tenant_fixture.openstack_tenant_service_settings)
        disconnected_subnet_url = factories.SubNetFactory.get_url(disconnected_subnet)
        floating_ip = self.openstack_tenant_fixture.floating_ip
        data = self.get_valid_data(
            floating_ips=[{'subnet': disconnected_subnet_url, 'url': factories.FloatingIPFactory.get_url(floating_ip)}],
        )

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_use_floating_ip_assigned_to_other_instance(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        internal_ip = factories.InternalIPFactory(subnet=self.subnet)
        floating_ip = factories.FloatingIPFactory(
            settings=self.openstack_settings,
            runtime_state='ACTIVE',
            internal_ip=internal_ip
        )
        data = self.get_valid_data(
            floating_ips=[{'subnet': subnet_url, 'url': factories.FloatingIPFactory.get_url(floating_ip)}],
        )

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('floating_ips', response.data)

    def test_user_can_assign_active_floating_ip(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        floating_ip = factories.FloatingIPFactory(settings=self.openstack_settings, runtime_state='ACTIVE')
        data = self.get_valid_data(
            floating_ips=[{'subnet': subnet_url, 'url': factories.FloatingIPFactory.get_url(floating_ip)}],
        )

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_can_allocate_floating_ip(self):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        self.openstack_tenant_fixture.floating_ip.status = 'ACTIVE'
        self.openstack_tenant_fixture.floating_ip.save()
        data = self.get_valid_data(
            floating_ips=[{'subnet': subnet_url}],
        )

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(uuid=response.data['uuid'])
        self.assertEqual(instance.floating_ips.count(), 1)

    def test_user_cannot_allocate_floating_ip_if_quota_limit_is_reached(self):
        self.openstack_settings.quotas.filter(name=self.openstack_settings.Quotas.floating_ip_count).update(limit=0)
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        self.openstack_tenant_fixture.floating_ip.status = 'ACTIVE'
        self.openstack_tenant_fixture.floating_ip.save()
        data = self.get_valid_data(
            floating_ips=[{'subnet': subnet_url}],
        )

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_create_instance_without_internal_ips(self):
        data = self.get_valid_data()
        del data['internal_ips_set']

        response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('internal_ips_set', response.data)


class InstanceDeleteTest(test_backend.BaseBackendTestCase):
    def setUp(self):
        super(InstanceDeleteTest, self).setUp()
        self.instance = factories.InstanceFactory(
            state=models.Instance.States.OK,
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
            backend_id='VALID_ID'
        )
        self.instance.increase_backend_quotas_usage()
        self.mocked_nova().servers.get.side_effect = nova_exceptions.NotFound(code=404)
        views.InstanceViewSet.async_executor = False

    def tearDown(self):
        super(InstanceDeleteTest, self).tearDown()
        views.InstanceViewSet.async_executor = True

    def mock_volumes(self, delete_data_volume=True):
        self.data_volume = self.instance.volumes.get(bootable=False)
        self.data_volume.backend_id = 'DATA_VOLUME_ID'
        self.data_volume.state = models.Volume.States.OK
        self.data_volume.save()
        self.data_volume.increase_backend_quotas_usage()

        self.system_volume = self.instance.volumes.get(bootable=True)
        self.system_volume.backend_id = 'SYSTEM_VOLUME_ID'
        self.system_volume.state = models.Volume.States.OK
        self.system_volume.save()
        self.system_volume.increase_backend_quotas_usage()

        def get_volume(backend_id):
            if not delete_data_volume and backend_id == self.data_volume.backend_id:
                mocked_volume = mock.Mock()
                mocked_volume.status = 'available'
                return mocked_volume
            raise cinder_exceptions.NotFound(code=404)

        self.mocked_cinder().volumes.get.side_effect = get_volume

    def delete_instance(self, query_params=None):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        url = factories.InstanceFactory.get_url(self.instance)
        if query_params:
            url += '?' + urllib.parse.urlencode(query_params)

        with override_settings(CELERY_ALWAYS_EAGER=True, CELERY_EAGER_PROPAGATES_EXCEPTIONS=True):
            response = self.client.delete(url)
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def assert_quota_usage(self, quotas, name, value):
        self.assertEqual(quotas.get(name=name).usage, value)

    def test_nova_methods_are_called_if_instance_is_deleted_with_volumes(self):
        self.mock_volumes(True)
        self.delete_instance()

        nova = self.mocked_nova()
        nova.servers.delete.assert_called_once_with(self.instance.backend_id)
        nova.servers.get.assert_called_once_with(self.instance.backend_id)

        self.assertFalse(nova.volumes.delete_server_volume.called)

    def test_database_models_deleted(self):
        self.mock_volumes(True)
        self.delete_instance()

        self.assertFalse(models.Instance.objects.filter(id=self.instance.id).exists())
        for volume in self.instance.volumes.all():
            self.assertFalse(models.Volume.objects.filter(id=volume.id).exists())

    def test_quotas_updated_if_instance_is_deleted_with_volumes(self):
        self.mock_volumes(True)
        self.delete_instance()

        self.instance.service_project_link.service.settings.refresh_from_db()
        quotas = self.instance.service_project_link.service.settings.quotas

        self.assert_quota_usage(quotas, 'instances', 0)
        self.assert_quota_usage(quotas, 'vcpu', 0)
        self.assert_quota_usage(quotas, 'ram', 0)

        self.assert_quota_usage(quotas, 'volumes', 0)
        self.assert_quota_usage(quotas, 'storage', 0)

    def test_backend_methods_are_called_if_instance_is_deleted_without_volumes(self):
        self.mock_volumes(False)
        self.delete_instance({
            'delete_volumes': False
        })

        nova = self.mocked_nova()
        nova.volumes.delete_server_volume.assert_called_once_with(
            self.instance.backend_id, self.data_volume.backend_id)

        nova.servers.delete.assert_called_once_with(self.instance.backend_id)
        nova.servers.get.assert_called_once_with(self.instance.backend_id)

    def test_system_volume_is_deleted_but_data_volume_exists(self):
        self.mock_volumes(False)
        self.delete_instance({
            'delete_volumes': False
        })

        self.assertFalse(models.Instance.objects.filter(id=self.instance.id).exists())
        self.assertTrue(models.Volume.objects.filter(id=self.data_volume.id).exists())
        self.assertFalse(models.Volume.objects.filter(id=self.system_volume.id).exists())

    def test_quotas_updated_if_instance_is_deleted_without_volumes(self):
        self.mock_volumes(False)
        self.delete_instance({
            'delete_volumes': False
        })

        settings = self.instance.service_project_link.service.settings
        settings.refresh_from_db()

        self.assert_quota_usage(settings.quotas, 'instances', 0)
        self.assert_quota_usage(settings.quotas, 'vcpu', 0)
        self.assert_quota_usage(settings.quotas, 'ram', 0)

        self.assert_quota_usage(settings.quotas, 'volumes', 1)
        self.assert_quota_usage(settings.quotas, 'storage', self.data_volume.size)

    def test_instance_cannot_be_deleted_if_it_has_backups(self):
        self.instance = factories.InstanceFactory(
            state=models.Instance.States.OK,
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
            backend_id='VALID_ID'
        )
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        factories.BackupFactory(instance=self.instance, state=models.Backup.States.OK)
        url = factories.InstanceFactory.get_url(self.instance)

        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    def test_neutron_methods_are_called_if_instance_is_deleted_with_floating_ips(self):
        fixture = fixtures.OpenStackTenantFixture()
        internal_ip = factories.InternalIPFactory.create(instance=self.instance, subnet=fixture.subnet)
        settings = self.instance.service_project_link.service.settings
        floating_ip = factories.FloatingIPFactory.create(internal_ip=internal_ip, settings=settings)
        self.delete_instance({'release_floating_ips': True})
        self.mocked_neutron().delete_floatingip.assert_called_once_with(floating_ip.backend_id)

    def test_neutron_methods_are_not_called_if_instance_does_not_have_any_floating_ips_yet(self):
        self.delete_instance({'release_floating_ips': True})
        self.assertEqual(self.mocked_neutron().delete_floatingip.call_count, 0)

    def test_neutron_methods_are_not_called_if_user_did_not_ask_for_floating_ip_removal_explicitly(self):
        self.mocked_neutron().show_floatingip.return_value = {'floatingip': {'status': 'DOWN'}}
        fixture = fixtures.OpenStackTenantFixture()
        internal_ip = factories.InternalIPFactory.create(instance=self.instance, subnet=fixture.subnet)
        settings = self.instance.service_project_link.service.settings
        factories.FloatingIPFactory.create(internal_ip=internal_ip, settings=settings)
        self.delete_instance({'release_floating_ips': False})
        self.assertEqual(self.mocked_neutron().delete_floatingip.call_count, 0)


class InstanceCreateBackupSchedule(test.APITransactionTestCase):
    action_name = 'create_backup_schedule'

    def setUp(self):
        self.user = structure_factories.UserFactory.create(is_staff=True)
        self.client.force_authenticate(user=self.user)
        backupable = factories.InstanceFactory(state=models.Instance.States.OK)
        self.create_url = factories.InstanceFactory.get_url(backupable, action=self.action_name)
        self.backup_schedule_data = {
            'name': 'test schedule',
            'retention_time': 3,
            'schedule': '0 * * * *',
            'maximal_number_of_resources': 3,
        }

    def test_staff_can_create_backup_schedule(self):
        response = self.client.post(self.create_url, self.backup_schedule_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['retention_time'], self.backup_schedule_data['retention_time'])
        self.assertEqual(
            response.data['maximal_number_of_resources'], self.backup_schedule_data['maximal_number_of_resources'])
        self.assertEqual(response.data['schedule'], self.backup_schedule_data['schedule'])

    def test_backup_schedule_default_state_is_OK(self):
        response = self.client.post(self.create_url, self.backup_schedule_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        backup_schedule = models.BackupSchedule.objects.first()
        self.assertIsNotNone(backup_schedule)
        self.assertEqual(backup_schedule.state, backup_schedule.States.OK)

    def test_backup_schedule_can_not_be_created_with_wrong_schedule(self):
        # wrong schedule:
        self.backup_schedule_data['schedule'] = 'wrong schedule'
        response = self.client.post(self.create_url, self.backup_schedule_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('schedule', response.content)

    def test_backup_schedule_creation_with_correct_timezone(self):
        backupable = factories.InstanceFactory(state=models.Instance.States.OK)
        create_url = factories.InstanceFactory.get_url(backupable, action=self.action_name)
        backup_schedule_data = {
            'name': 'test schedule',
            'retention_time': 3,
            'schedule': '0 * * * *',
            'timezone': 'Europe/London',
            'maximal_number_of_resources': 3,
        }
        response = self.client.post(create_url, backup_schedule_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['timezone'], 'Europe/London')

    def test_backup_schedule_creation_with_incorrect_timezone(self):
        backupable = factories.InstanceFactory(state=models.Instance.States.OK)
        create_url = factories.InstanceFactory.get_url(backupable, action=self.action_name)

        backup_schedule_data = {
            'name': 'test schedule',
            'retention_time': 3,
            'schedule': '0 * * * *',
            'timezone': 'incorrect',
            'maximal_number_of_resources': 3,
        }
        response = self.client.post(create_url, backup_schedule_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('timezone', response.data)

    def test_backup_schedule_creation_with_default_timezone(self):
        backupable = factories.InstanceFactory(state=models.Instance.States.OK)
        create_url = factories.InstanceFactory.get_url(backupable, action=self.action_name)
        backup_schedule_data = {
            'name': 'test schedule',
            'retention_time': 3,
            'schedule': '0 * * * *',
            'maximal_number_of_resources': 3,
        }
        response = self.client.post(create_url, backup_schedule_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['timezone'], settings.TIME_ZONE)


class InstanceUpdateInternalIPsSetTest(test.APITransactionTestCase):
    action_name = 'update_internal_ips_set'

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.client.force_authenticate(user=self.fixture.admin)
        self.instance = self.fixture.instance
        self.url = factories.InstanceFactory.get_url(self.instance, action=self.action_name)

    def test_user_can_update_instance_internal_ips_set(self):
        # instance had 2 internal IPs
        ip_to_keep = factories.InternalIPFactory(instance=self.instance, subnet=self.fixture.subnet)
        ip_to_delete = factories.InternalIPFactory(instance=self.instance)
        # instance should be connected to new subnet
        subnet_to_connect = factories.SubNetFactory(settings=self.fixture.openstack_tenant_service_settings)

        response = self.client.post(self.url, data={
            'internal_ips_set': [
                {'subnet': factories.SubNetFactory.get_url(self.fixture.subnet)},
                {'subnet': factories.SubNetFactory.get_url(subnet_to_connect)},
            ]
        })

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(self.instance.internal_ips_set.filter(pk=ip_to_keep.pk).exists())
        self.assertFalse(self.instance.internal_ips_set.filter(pk=ip_to_delete.pk).exists())
        self.assertTrue(self.instance.internal_ips_set.filter(subnet=subnet_to_connect).exists())

    def test_user_cannot_add_intenal_ip_from_different_settings(self):
        subnet = factories.SubNetFactory()

        response = self.client.post(self.url, data={
            'internal_ips_set': [
                {'subnet': factories.SubNetFactory.get_url(subnet)},
            ]
        })

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.instance.internal_ips_set.filter(subnet=subnet).exists())

    def test_user_cannot_connect_instance_to_one_subnet_twice(self):
        response = self.client.post(self.url, data={
            'internal_ips_set': [
                {'subnet': factories.SubNetFactory.get_url(self.fixture.subnet)},
                {'subnet': factories.SubNetFactory.get_url(self.fixture.subnet)},
            ]
        })

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.instance.internal_ips_set.filter(subnet=self.fixture.subnet).exists())


class InstanceUpdateFloatingIPsTest(test.APITransactionTestCase):
    action_name = 'update_floating_ips'

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.fixture.openstack_tenant_service_settings.options = {'external_network_id': uuid.uuid4().hex}
        self.fixture.openstack_tenant_service_settings.save()
        self.client.force_authenticate(user=self.fixture.admin)
        self.instance = self.fixture.instance
        factories.InternalIPFactory.create(instance=self.instance, subnet=self.fixture.subnet)
        self.url = factories.InstanceFactory.get_url(self.instance, action=self.action_name)
        self.subnet_url = factories.SubNetFactory.get_url(self.fixture.subnet)

    def test_user_can_update_instance_floating_ips(self):
        floating_ip_url = factories.FloatingIPFactory.get_url(self.fixture.floating_ip)
        data = {
            'floating_ips': [
                {'subnet': self.subnet_url, 'url': floating_ip_url},
            ]
        }

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.instance.floating_ips.count(), 1)
        self.assertIn(self.fixture.floating_ip, self.instance.floating_ips)

    def test_when_floating_ip_is_attached_action_details_are_updated(self):
        floating_ip_url = factories.FloatingIPFactory.get_url(self.fixture.floating_ip)
        data = {
            'floating_ips': [
                {'subnet': self.subnet_url, 'url': floating_ip_url},
            ]
        }

        self.client.post(self.url, data=data)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.action_details, {
            'message': 'Attached floating IPs: %s.' % self.fixture.floating_ip.address,
            'attached': [self.fixture.floating_ip.address],
            'detached': [],
        })

    def test_when_floating_ip_is_detached_action_details_are_updated(self):
        self.fixture.floating_ip.internal_ip = self.instance.internal_ips_set.first()
        self.fixture.floating_ip.save()

        self.client.post(self.url, data={
            'floating_ips': []
        })

        self.instance.refresh_from_db()
        self.assertEqual(self.instance.action_details, {
            'message': 'Detached floating IPs: %s.' % self.fixture.floating_ip.address,
            'attached': [],
            'detached': [self.fixture.floating_ip.address],
        })

    def test_user_can_not_assign_floating_ip_used_by_other_instance(self):
        internal_ip = factories.InternalIPFactory(subnet=self.fixture.subnet)
        floating_ip = factories.FloatingIPFactory(
            settings=self.fixture.openstack_tenant_service_settings,
            runtime_state='DOWN',
            internal_ip=internal_ip,
        )
        floating_ip_url = factories.FloatingIPFactory.get_url(floating_ip)
        data = {
            'floating_ips': [
                {'subnet': self.subnet_url, 'url': floating_ip_url},
            ]
        }

        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('floating_ips', response.data)

    def test_user_cannot_add_floating_ip_via_subnet_that_is_not_connected_to_instance(self):
        subnet_url = factories.SubNetFactory.get_url()
        data = {'floating_ips': [{'subnet': subnet_url}]}

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_remove_floating_ip_from_instance(self):
        self.fixture.floating_ip.internal_ip = self.instance.internal_ips_set.first()
        self.fixture.floating_ip.save()
        data = {'floating_ips': []}

        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.instance.floating_ips.count(), 0)

    def test_free_floating_ip_is_used_for_allocation(self):
        external_network_id = self.fixture.openstack_tenant_service_settings.options['external_network_id']
        self.fixture.floating_ip.backend_network_id = external_network_id
        self.fixture.floating_ip.save()
        data = {'floating_ips': [{'subnet': self.subnet_url}]}

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn(self.fixture.floating_ip, self.instance.floating_ips)

    def test_user_cannot_use_same_subnet_twice(self):
        data = {'floating_ips': [{'subnet': self.subnet_url}, {'subnet': self.subnet_url}]}
        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class InstanceBackupTest(test.APITransactionTestCase):
    action_name = 'backup'

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.client.force_authenticate(self.fixture.owner)

    def test_backup_can_be_created_for_instance_with_2_volumes(self):
        url = factories.InstanceFactory.get_url(self.fixture.instance, action='backup')
        payload = self.get_payload()
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Backup.objects.get(name=payload['name']).snapshots.count(), 2)

    def test_backup_can_be_created_for_instance_only_with_system_volume(self):
        instance = self.fixture.instance
        instance.volumes.filter(bootable=False).delete()
        url = factories.InstanceFactory.get_url(instance, action='backup')
        payload = self.get_payload()
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.Backup.objects.get(name=payload['name']).snapshots.count(), 1)

    def test_backup_can_be_created_for_instance_with_3_volumes(self):
        instance = self.fixture.instance
        instance.volumes.add(factories.VolumeFactory(service_project_link=instance.service_project_link))
        url = factories.InstanceFactory.get_url(instance, action='backup')
        payload = self.get_payload()
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.Backup.objects.get(name=payload['name']).snapshots.count(), 3)

    def test_user_cannot_backup_unstable_instance(self):
        instance = self.fixture.instance
        instance.state = models.Instance.States.UPDATING
        instance.save()
        url = factories.InstanceFactory.get_url(instance, action='backup')

        response = self.client.post(url, data={'name': 'test backup'})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def get_payload(self):
        return {
            'name': 'backup_name'
        }


class BaseInstanceImportTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()

    def _generate_backend_instances(self, count=1):
        instances = []
        for i in range(count):
            instance = factories.InstanceFactory()
            instance.delete()
            instances.append(instance)

        return instances


class InstanceImportableResourcesTest(BaseInstanceImportTest):

    def setUp(self):
        super(InstanceImportableResourcesTest, self).setUp()
        self.url = factories.InstanceFactory.get_list_url('importable_resources')
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.get_instances_for_import')
    def test_importable_instances_are_returned(self, get_instances_for_import_mock):
        backend_instances = self._generate_backend_instances()
        get_instances_for_import_mock.return_value = backend_instances
        data = {'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl)}

        response = self.client.get(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEquals(len(response.data), len(backend_instances))
        returned_backend_ids = [item['backend_id'] for item in response.data]
        expected_backend_ids = [item.backend_id for item in backend_instances]
        self.assertItemsEqual(returned_backend_ids, expected_backend_ids)
        get_instances_for_import_mock.assert_called()


class InstanceImportTest(BaseInstanceImportTest):

    def setUp(self):
        super(InstanceImportTest, self).setUp()
        self.url = factories.InstanceFactory.get_list_url('import_resource')
        self.client.force_authenticate(self.fixture.owner)

    def _get_payload(self, backend_id):
        return {
            'backend_id': backend_id,
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl),
        }

    @mock.patch('waldur_openstack.openstack_tenant.executors.InstancePullExecutor.execute')
    @mock.patch('waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.import_instance')
    def test_instance_can_be_imported(self, import_instance_mock, resource_import_execute_mock):
        backend_id = 'backend_id'

        def import_instance(backend_id, save, service_project_link):
            return self._generate_backend_instances()[0]

        import_instance_mock.side_effect = import_instance
        payload = self._get_payload(backend_id)

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        resource_import_execute_mock.assert_called()

    def test_existing_instance_cannot_be_imported(self):
        payload = self._get_payload(factories.InstanceFactory().backend_id)

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
