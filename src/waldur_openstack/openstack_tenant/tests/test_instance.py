import uuid
from unittest import mock
from urllib.parse import urlencode

from celery import Signature
from ddt import data, ddt
from django.conf import settings
from django.test import override_settings
from novaclient import exceptions as nova_exceptions
from rest_framework import status, test

from waldur_core.core.utils import serialize_instance
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack.openstack.tests.unittests import test_backend
from waldur_openstack.openstack_base.backend import OpenStackBackendError

from .. import executors, models, views
from . import factories, fixtures, helpers


class InstanceFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.client.force_authenticate(user=self.fixture.owner)
        self.url = factories.InstanceFactory.get_list_url()

    def test_filter_instance_by_valid_volume_uuid(self):
        self.fixture.instance
        response = self.client.get(
            self.url, {'attach_volume_uuid': self.fixture.volume.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)

    def test_filter_instance_by_invalid_volume_uuid(self):
        self.fixture.instance
        response = self.client.get(self.url, {'attach_volume_uuid': 'invalid'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_instance_by_availability_zone(self):
        vm_az = self.fixture.instance_availability_zone
        vm = self.fixture.instance
        vm.availability_zone = vm_az
        vm.save()

        volume_az = self.fixture.volume_availability_zone
        volume = self.fixture.volume
        volume.availability_zone = volume_az
        volume.save()

        private_settings = self.fixture.openstack_tenant_service_settings
        shared_settings = private_settings.scope.service_settings

        shared_settings.options = {
            'valid_availability_zones': {vm_az.name: volume_az.name}
        }
        shared_settings.save()

        response = self.client.get(self.url, {'attach_volume_uuid': volume.uuid.hex})
        self.assertEqual(len(response.data), 1)


class InstanceCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.openstack_tenant_fixture = fixtures.OpenStackTenantFixture()
        self.openstack_settings = (
            self.openstack_tenant_fixture.openstack_tenant_service_settings
        )
        self.openstack_settings.options = {'external_network_id': uuid.uuid4().hex}
        self.openstack_settings.save()
        self.openstack_spl = self.openstack_tenant_fixture.spl
        self.project = self.openstack_tenant_fixture.project
        self.customer = self.openstack_tenant_fixture.customer
        self.image = factories.ImageFactory(
            settings=self.openstack_settings, min_disk=10240, min_ram=1024
        )
        self.flavor = factories.FlavorFactory(settings=self.openstack_settings)
        self.subnet = self.openstack_tenant_fixture.subnet

        self.client.force_authenticate(user=self.openstack_tenant_fixture.owner)
        self.url = factories.InstanceFactory.get_list_url()

    def get_valid_data(self, **extra):
        subnet_url = factories.SubNetFactory.get_url(self.subnet)
        default = {
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(
                self.openstack_spl
            ),
            'flavor': factories.FlavorFactory.get_url(self.flavor),
            'image': factories.ImageFactory.get_url(self.image),
            'name': 'valid-name',
            'system_volume_size': self.image.min_disk,
            'internal_ips_set': [{'subnet': subnet_url}],
        }
        default.update(extra)
        return default

    def test_show_volume_type_in_instance_serializer(self):
        instance = factories.InstanceFactory()
        settings = instance.service_project_link.service.settings
        volume_type = factories.VolumeTypeFactory(settings=settings)
        factories.VolumeFactory(
            service_project_link=instance.service_project_link,
            instance=instance,
            type=volume_type,
            name='test-volume',
        )
        url = factories.InstanceFactory.get_url(instance)
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        serialized_volume = [
            volume
            for volume in response.data['volumes']
            if volume['name'] == 'test-volume'
        ][0]
        self.assertEqual(serialized_volume['type_name'], volume_type.name)


class InstanceDeleteTest(test_backend.BaseBackendTestCase):
    def setUp(self):
        super(InstanceDeleteTest, self).setUp()
        self.instance = factories.InstanceFactory(
            state=models.Instance.States.OK,
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
            backend_id='VALID_ID',
        )
        self.instance.increase_backend_quotas_usage()
        self.mocked_nova().servers.get.side_effect = nova_exceptions.NotFound(code=404)
        views.InstanceViewSet.async_executor = False

    def tearDown(self):
        super(InstanceDeleteTest, self).tearDown()
        views.InstanceViewSet.async_executor = True

    def delete_instance(self, query_params=None):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        url = factories.InstanceFactory.get_url(self.instance)
        if query_params:
            url += '?' + urlencode(query_params)

        with override_settings(
            CELERY_ALWAYS_EAGER=True, CELERY_EAGER_PROPAGATES_EXCEPTIONS=True
        ):
            response = self.client.delete(url)
            self.assertEqual(
                response.status_code, status.HTTP_202_ACCEPTED, response.data
            )

    def assert_quota_usage(self, quotas, name, value):
        self.assertEqual(quotas.get(name=name).usage, value)

    def test_incomplete_instance_deletion_executor_produces_celery_signature(self):
        # Arrange
        self.instance.backend_id = None
        self.instance.save()

        # Act
        serialized_instance = serialize_instance(self.instance)
        signature = executors.InstanceDeleteExecutor.get_task_signature(
            self.instance, serialized_instance
        )

        # Assert
        self.assertIsInstance(signature, Signature)

    @mock.patch(
        'waldur_openstack.openstack_tenant.views.executors.InstanceDeleteExecutor'
    )
    def test_force_delete_instance(self, mock_delete_executor):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        self.instance.runtime_state = models.Instance.RuntimeStates.ACTIVE
        self.instance.save()

        url = factories.InstanceFactory.get_url(self.instance, 'force_destroy')
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(mock_delete_executor.execute.call_count, 1)


class InstanceCreateBackupSchedule(test.APITransactionTestCase):
    action_name = 'create_backup_schedule'

    def setUp(self):
        self.user = structure_factories.UserFactory.create(is_staff=True)
        self.client.force_authenticate(user=self.user)
        self.instance = factories.InstanceFactory(state=models.Instance.States.OK)
        self.create_url = factories.InstanceFactory.get_url(
            self.instance, action=self.action_name
        )
        self.backup_schedule_data = {
            'name': 'test schedule',
            'retention_time': 3,
            'schedule': '0 * * * *',
            'maximal_number_of_resources': 3,
        }

    def test_staff_can_create_backup_schedule(self):
        response = self.client.post(self.create_url, self.backup_schedule_data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data['retention_time'], self.backup_schedule_data['retention_time']
        )
        self.assertEqual(
            response.data['maximal_number_of_resources'],
            self.backup_schedule_data['maximal_number_of_resources'],
        )
        self.assertEqual(
            response.data['schedule'], self.backup_schedule_data['schedule']
        )

    def test_instance_should_have_bootable_volume(self):
        self.instance.volumes.filter(bootable=True).delete()
        response = self.client.post(self.create_url, self.backup_schedule_data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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
        self.assertIn(b'schedule', response.content)

    def test_backup_schedule_creation_with_correct_timezone(self):
        backupable = factories.InstanceFactory(state=models.Instance.States.OK)
        create_url = factories.InstanceFactory.get_url(
            backupable, action=self.action_name
        )
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
        create_url = factories.InstanceFactory.get_url(
            backupable, action=self.action_name
        )

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
        create_url = factories.InstanceFactory.get_url(
            backupable, action=self.action_name
        )
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
        self.url = factories.InstanceFactory.get_url(
            self.instance, action=self.action_name
        )

    def test_user_can_update_instance_internal_ips_set(self):
        # instance had 2 internal IPs
        ip_to_keep = factories.InternalIPFactory(
            instance=self.instance, subnet=self.fixture.subnet
        )
        ip_to_delete = factories.InternalIPFactory(instance=self.instance)
        # instance should be connected to new subnet
        subnet_to_connect = factories.SubNetFactory(
            settings=self.fixture.openstack_tenant_service_settings
        )

        response = self.client.post(
            self.url,
            data={
                'internal_ips_set': [
                    {'subnet': factories.SubNetFactory.get_url(self.fixture.subnet)},
                    {'subnet': factories.SubNetFactory.get_url(subnet_to_connect)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(
            self.instance.internal_ips_set.filter(pk=ip_to_keep.pk).exists()
        )
        self.assertFalse(
            self.instance.internal_ips_set.filter(pk=ip_to_delete.pk).exists()
        )
        self.assertTrue(
            self.instance.internal_ips_set.filter(subnet=subnet_to_connect).exists()
        )

    def test_user_cannot_add_intenal_ip_from_different_settings(self):
        subnet = factories.SubNetFactory()

        response = self.client.post(
            self.url,
            data={
                'internal_ips_set': [
                    {'subnet': factories.SubNetFactory.get_url(subnet)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.instance.internal_ips_set.filter(subnet=subnet).exists())

    def test_user_cannot_connect_instance_to_one_subnet_twice(self):
        response = self.client.post(
            self.url,
            data={
                'internal_ips_set': [
                    {'subnet': factories.SubNetFactory.get_url(self.fixture.subnet)},
                    {'subnet': factories.SubNetFactory.get_url(self.fixture.subnet)},
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            self.instance.internal_ips_set.filter(subnet=self.fixture.subnet).exists()
        )


class InstanceUpdateFloatingIPsTest(test.APITransactionTestCase):
    action_name = 'update_floating_ips'

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.fixture.openstack_tenant_service_settings.options = {
            'external_network_id': uuid.uuid4().hex
        }
        self.fixture.openstack_tenant_service_settings.save()
        self.client.force_authenticate(user=self.fixture.admin)
        self.instance = self.fixture.instance
        factories.InternalIPFactory.create(
            instance=self.instance, subnet=self.fixture.subnet
        )
        self.url = factories.InstanceFactory.get_url(
            self.instance, action=self.action_name
        )
        self.subnet_url = factories.SubNetFactory.get_url(self.fixture.subnet)

    def test_user_can_update_instance_floating_ips(self):
        floating_ip_url = factories.FloatingIPFactory.get_url(self.fixture.floating_ip)
        data = {'floating_ips': [{'subnet': self.subnet_url, 'url': floating_ip_url},]}

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.instance.floating_ips.count(), 1)
        self.assertIn(self.fixture.floating_ip, self.instance.floating_ips)

    def test_when_floating_ip_is_attached_action_details_are_updated(self):
        floating_ip_url = factories.FloatingIPFactory.get_url(self.fixture.floating_ip)
        data = {'floating_ips': [{'subnet': self.subnet_url, 'url': floating_ip_url},]}

        self.client.post(self.url, data=data)
        self.instance.refresh_from_db()
        self.assertEqual(
            self.instance.action_details,
            {
                'message': 'Attached floating IPs: %s.'
                % self.fixture.floating_ip.address,
                'attached': [self.fixture.floating_ip.address],
                'detached': [],
            },
        )

    def test_when_floating_ip_is_detached_action_details_are_updated(self):
        self.fixture.floating_ip.internal_ip = self.instance.internal_ips_set.first()
        self.fixture.floating_ip.save()

        self.client.post(self.url, data={'floating_ips': []})

        self.instance.refresh_from_db()
        self.assertEqual(
            self.instance.action_details,
            {
                'message': 'Detached floating IPs: %s.'
                % self.fixture.floating_ip.address,
                'attached': [],
                'detached': [self.fixture.floating_ip.address],
            },
        )

    def test_user_can_not_assign_floating_ip_used_by_other_instance(self):
        internal_ip = factories.InternalIPFactory(subnet=self.fixture.subnet)
        floating_ip = factories.FloatingIPFactory(
            settings=self.fixture.openstack_tenant_service_settings,
            runtime_state='DOWN',
            internal_ip=internal_ip,
        )
        floating_ip_url = factories.FloatingIPFactory.get_url(floating_ip)
        data = {'floating_ips': [{'subnet': self.subnet_url, 'url': floating_ip_url},]}

        response = self.client.post(self.url, data=data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('floating_ips', response.data)

    def test_user_cannot_add_floating_ip_via_subnet_that_is_not_connected_to_instance(
        self,
    ):
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
        external_network_id = self.fixture.openstack_tenant_service_settings.options[
            'external_network_id'
        ]
        self.fixture.floating_ip.backend_network_id = external_network_id
        self.fixture.floating_ip.save()
        data = {'floating_ips': [{'subnet': self.subnet_url}]}

        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn(self.fixture.floating_ip, self.instance.floating_ips)

    def test_user_cannot_use_same_subnet_twice(self):
        data = {
            'floating_ips': [{'subnet': self.subnet_url}, {'subnet': self.subnet_url}]
        }
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
        self.assertEqual(
            models.Backup.objects.get(name=payload['name']).snapshots.count(), 2
        )

    def test_backup_can_be_created_for_instance_only_with_system_volume(self):
        instance = self.fixture.instance
        instance.volumes.filter(bootable=False).delete()
        url = factories.InstanceFactory.get_url(instance, action='backup')
        payload = self.get_payload()
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            models.Backup.objects.get(name=payload['name']).snapshots.count(), 1
        )

    def test_backup_can_be_created_for_instance_with_3_volumes(self):
        instance = self.fixture.instance
        instance.volumes.add(
            factories.VolumeFactory(service_project_link=instance.service_project_link)
        )
        url = factories.InstanceFactory.get_url(instance, action='backup')
        payload = self.get_payload()
        response = self.client.post(url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            models.Backup.objects.get(name=payload['name']).snapshots.count(), 3
        )

    def test_user_cannot_backup_unstable_instance(self):
        instance = self.fixture.instance
        instance.state = models.Instance.States.UPDATING
        instance.save()
        url = factories.InstanceFactory.get_url(instance, action='backup')

        response = self.client.post(url, data={'name': 'test backup'})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def get_payload(self):
        return {'name': 'backup_name'}


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

    @mock.patch(
        'waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.get_instances_for_import'
    )
    def test_importable_instances_are_returned(self, get_instances_for_import_mock):
        backend_instances = self._generate_backend_instances()
        get_instances_for_import_mock.return_value = backend_instances
        data = {
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(
                self.fixture.spl
            )
        }

        response = self.client.get(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEquals(len(response.data), len(backend_instances))
        returned_backend_ids = [item['backend_id'] for item in response.data]
        expected_backend_ids = [item.backend_id for item in backend_instances]
        self.assertEqual(sorted(returned_backend_ids), sorted(expected_backend_ids))
        get_instances_for_import_mock.assert_called()


class InstanceImportTest(BaseInstanceImportTest):
    def setUp(self):
        super(InstanceImportTest, self).setUp()
        self.url = factories.InstanceFactory.get_list_url('import_resource')
        self.client.force_authenticate(self.fixture.owner)

    def _get_payload(self, backend_id):
        return {
            'backend_id': backend_id,
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(
                self.fixture.spl
            ),
        }

    @mock.patch(
        'waldur_openstack.openstack_tenant.executors.InstancePullExecutor.execute'
    )
    @mock.patch(
        'waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.import_instance'
    )
    def test_instance_can_be_imported(
        self, import_instance_mock, resource_import_execute_mock
    ):
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

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )


@ddt
class InstanceActionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.fixture.openstack_tenant_service_settings.options = {
            'external_network_id': uuid.uuid4().hex,
            'tenant_id': self.fixture.tenant.id,
        }
        self.fixture.openstack_tenant_service_settings.save()
        self.instance = self.fixture.instance

        self.url = factories.InstanceFactory.get_url(self.instance, action=self.action)
        self.mock_path = mock.patch(
            'waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.%s'
            % self.backend_method
        )
        self.mock_console = self.mock_path.start()
        self.mock_console.return_value = self.backend_return_value

    def tearDown(self):
        super(InstanceActionsTest, self).tearDown()
        mock.patch.stopall()


@ddt
class InstanceConsoleTest(InstanceActionsTest):
    action = 'console'
    backend_method = 'get_console_url'
    backend_return_value = 'url'

    @data('staff')
    def test_action_available_to_staff(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mock_console.assert_called_once_with(self.instance)

    @data('admin', 'manager', 'owner')
    def test_action_not_available_for_users(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'admin', 'manager', 'owner')
    @helpers.override_openstack_tenant_settings(
        ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS=True
    )
    def test_action_available_for_users_if_this_allowed_in_settings(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('user')
    @helpers.override_openstack_tenant_settings(
        ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS=True
    )
    def test_action_not_available_for_other_users(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_error_is_propagated_correctly(self):
        self.mock_console.side_effect = OpenStackBackendError('Invalid request.')
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('Invalid request.' in response.data)


@ddt
class InstanceConsoleLogTest(InstanceActionsTest):
    action = 'console_log'
    backend_method = 'get_console_output'
    backend_return_value = 'openstack-vm login: '

    @data('staff', 'admin', 'manager', 'owner')
    def test_action_available_for_staff_and_users_associated_with_project(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.mock_console.assert_called_once_with(self.instance, None)

    @data('user')
    def test_action_not_available_for_users_unassociated_with_project(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_error_is_propagated_correctly(self):
        self.mock_console.side_effect = OpenStackBackendError('Invalid request.')
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('Invalid request.' in response.data)
