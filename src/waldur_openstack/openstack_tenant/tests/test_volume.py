from unittest import mock

from ddt import data, ddt
from django.conf import settings
from rest_framework import status, test

from waldur_openstack.openstack_tenant import models
from waldur_openstack.openstack_tenant.tests.helpers import (
    override_openstack_tenant_settings,
)

from . import factories, fixtures


class VolumeDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.volume = self.fixture.volume
        self.spl = self.fixture.spl

    def destroy_volume(self):
        url = factories.VolumeFactory.get_url(self.volume)
        self.client.force_authenticate(self.fixture.staff)
        return self.client.delete(url)

    def test_spl_quota_updated_by_signal_handler_when_volume_is_removed(self):
        self.volume.delete()
        Quotas = models.OpenStackTenantServiceProjectLink.Quotas
        self.assertEqual(self.spl.quotas.get(name=Quotas.storage).usage, 0)

    def test_erred_volume_can_be_destroyed(self):
        self.volume.state = models.Volume.States.ERRED
        self.volume.save()
        response = self.destroy_volume()
        self.assertEqual(response.status_code, 202)

    def test_attached_volume_can_not_be_destroyed(self):
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'in-use'
        self.volume.save()
        response = self.destroy_volume()
        self.assertEqual(response.status_code, 409)

    def test_pending_volume_can_not_be_destroyed(self):
        self.volume.state = models.Volume.States.CREATING
        self.volume.save()
        response = self.destroy_volume()
        self.assertEqual(response.status_code, 409)


@ddt
class VolumeExtendTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        self.staff = self.fixture.staff
        self.volume = self.fixture.volume

    def extend_disk(self, user, new_size):
        url = factories.VolumeFactory.get_url(self.volume, action='extend')
        self.client.force_authenticate(user)
        return self.client.post(url, {'disk_size': new_size})

    @data('admin', 'manager')
    def test_user_can_resize_size_of_volume_he_has_access_to(self, user):
        new_size = self.volume.size + 1024

        response = self.extend_disk(getattr(self, user), new_size)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        self.volume.refresh_from_db()
        self.assertEqual(self.volume.size, new_size)

    def test_user_can_not_extend_volume_if_resulting_quota_usage_is_greater_than_limit(
        self,
    ):
        service_settings = self.volume.service_project_link.service.settings
        service_settings.set_quota_usage('storage', self.volume.size)
        service_settings.set_quota_limit('storage', self.volume.size + 512)

        new_size = self.volume.size + 1024
        response = self.extend_disk(self.admin, new_size)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_not_extend_volume_if_quota_usage_becomes_greater_than_limit(
        self,
    ):
        scope = self.volume.service_project_link.service.settings
        scope.set_quota_usage('storage', self.volume.size)
        scope.set_quota_limit('storage', self.volume.size + 512)

        new_size = self.volume.size + 1024
        response = self.extend_disk(self.admin, new_size)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_not_extend_volume_if_volume_operation_is_performed(self):
        self.volume.state = models.Volume.States.UPDATING
        self.volume.save()

        new_size = self.volume.size + 1024
        response = self.extend_disk(self.admin, new_size)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_can_not_extend_volume_if_volume_is_in_erred_state(self):
        self.volume.state = models.Instance.States.ERRED
        self.volume.save()

        new_size = self.volume.size + 1024
        response = self.extend_disk(self.admin, new_size)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_when_volume_is_extended_volume_type_quota_is_updated(self):
        # Arrange
        private_settings = self.volume.service_project_link.service.settings
        shared_tenant = private_settings.scope
        key = 'gigabytes_' + self.volume.type.backend_id

        private_settings.set_quota_usage(key, self.volume.size / 1024)
        shared_tenant.set_quota_usage(key, self.volume.size / 1024)

        # Act
        new_size = self.volume.size + 1024
        self.extend_disk(self.staff, new_size)

        # Assert
        self.assertEqual(new_size / 1024, private_settings.quotas.get(name=key).usage)
        self.assertEqual(new_size / 1024, shared_tenant.quotas.get(name=key).usage)


class VolumeAttachTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.volume = self.fixture.volume
        self.instance = self.fixture.instance
        self.url = factories.VolumeFactory.get_url(self.volume, action='attach')

    def get_response(self):
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {'instance': factories.InstanceFactory.get_url(self.instance)}
        return self.client.post(self.url, payload)

    def test_user_can_attach_volume_to_instance(self):
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'available'
        self.volume.save()

        self.instance.state = models.Instance.States.OK
        self.instance.runtime_state = models.Instance.RuntimeStates.SHUTOFF
        self.instance.save()

        response = self.get_response()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_user_can_not_attach_erred_volume_to_instance(self):
        self.volume.state = models.Volume.States.ERRED
        self.volume.save()

        response = self.get_response()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_can_not_attach_used_volume_to_instance(self):
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'in-use'
        self.volume.save()

        response = self.get_response()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_can_not_attach_volume_to_instance_in_other_tenant(self):
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'available'
        self.volume.save()
        self.instance = factories.InstanceFactory()

        response = self.get_response()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_attach_volume_to_instance_in_active_state(self):
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'available'
        self.volume.save()

        self.instance.state = models.Instance.States.OK
        self.instance.runtime_state = models.Instance.RuntimeStates.ACTIVE
        self.instance.save()

        response = self.get_response()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_volume_AZ_should_match_instance_AZ(self):
        volume_az = self.fixture.volume_availability_zone
        self.volume.availability_zone = volume_az
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'available'
        self.volume.save()

        instance_az = self.fixture.instance_availability_zone
        self.instance.availability_zone = instance_az
        self.instance.state = models.Instance.States.OK
        self.instance.runtime_state = models.Instance.RuntimeStates.ACTIVE
        self.instance.save()

        private_settings = self.fixture.openstack_tenant_service_settings
        shared_settings = private_settings.scope.service_settings

        shared_settings.options = {
            'valid_availability_zones': {instance_az.name: volume_az.name}
        }
        shared_settings.save()

        response = self.get_response()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)


class VolumeSnapshotTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.volume = self.fixture.volume
        self.url = factories.VolumeFactory.get_url(self.volume, action='snapshot')
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'available'
        self.volume.save()

    def create_snapshot(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = {'name': '%s snapshot' % self.volume.name}
        return self.client.post(self.url, data=payload)

    def test_user_can_create_volume_snapshot(self):
        response = self.create_snapshot()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_when_snapshot_is_created_volume_type_quota_is_updated(self):
        self.create_snapshot()
        key = 'gigabytes_' + self.fixture.volume_type.backend_id
        scope = self.fixture.openstack_tenant_service_settings
        usage = scope.quotas.get(name=key).usage
        self.assertEqual(self.volume.size / 1024, usage)


@ddt
class VolumeCreateSnapshotScheduleTest(test.APITransactionTestCase):
    action_name = 'create_snapshot_schedule'

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.url = factories.VolumeFactory.get_url(
            self.fixture.volume, self.action_name
        )
        self.snapshot_schedule_data = {
            'name': 'test schedule',
            'retention_time': 3,
            'schedule': '0 * * * *',
            'maximal_number_of_resources': 3,
        }

    @data('owner', 'staff', 'admin', 'manager')
    def test_user_can_create_snapshot_schedule(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, self.snapshot_schedule_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data['retention_time'],
            self.snapshot_schedule_data['retention_time'],
        )
        self.assertEqual(
            response.data['maximal_number_of_resources'],
            self.snapshot_schedule_data['maximal_number_of_resources'],
        )
        self.assertEqual(
            response.data['schedule'], self.snapshot_schedule_data['schedule']
        )

    def test_snapshot_schedule_cannot_be_created_if_schedule_is_less_than_1_hours(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = self.snapshot_schedule_data
        payload['schedule'] = '*/5 * * * *'

        response = self.client.post(self.url, self.snapshot_schedule_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('schedule', response.data)

    def test_snapshot_schedule_default_state_is_OK(self):
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(self.url, self.snapshot_schedule_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        snapshot_schedule = models.SnapshotSchedule.objects.first()
        self.assertIsNotNone(snapshot_schedule)
        self.assertEqual(snapshot_schedule.state, snapshot_schedule.States.OK)

    def test_snapshot_schedule_can_not_be_created_with_wrong_schedule(self):
        self.client.force_authenticate(self.fixture.owner)

        # wrong schedule:
        self.snapshot_schedule_data['schedule'] = 'wrong schedule'

        response = self.client.post(self.url, self.snapshot_schedule_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(b'schedule', response.content)

    def test_snapshot_schedule_creation_with_correct_timezone(self):
        self.client.force_authenticate(self.fixture.owner)
        expected_timezone = 'Europe/London'
        self.snapshot_schedule_data['timezone'] = expected_timezone
        response = self.client.post(self.url, self.snapshot_schedule_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['timezone'], expected_timezone)

    def test_snapshot_schedule_creation_with_incorrect_timezone(self):
        self.client.force_authenticate(self.fixture.owner)
        self.snapshot_schedule_data['timezone'] = 'incorrect'
        response = self.client.post(self.url, self.snapshot_schedule_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('timezone', response.data)

    def test_snapshot_schedule_creation_with_default_timezone(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, self.snapshot_schedule_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['timezone'], settings.TIME_ZONE)


class BaseVolumeTest(test.APITransactionTestCase):
    def _generate_backend_volumes(self, count=1):
        volumes = []
        for i in range(count):
            volume = factories.VolumeFactory()
            volume.delete()
            volumes.append(volume)

        return volumes


class VolumeImportableResourcesTest(BaseVolumeTest):
    def setUp(self):
        super(VolumeImportableResourcesTest, self).setUp()
        self.url = factories.VolumeFactory.get_list_url('importable_resources')
        self.fixture = fixtures.OpenStackTenantFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch(
        'waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.get_volumes_for_import'
    )
    def test_importable_volumes_are_returned(self, get_volumes_mock):
        backend_volumes = self._generate_backend_volumes()
        get_volumes_mock.return_value = backend_volumes
        data = {
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(
                self.fixture.spl
            )
        }

        response = self.client.get(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEquals(len(response.data), len(backend_volumes))
        returned_backend_ids = [item['backend_id'] for item in response.data]
        expected_backend_ids = [item.backend_id for item in backend_volumes]
        self.assertEqual(sorted(returned_backend_ids), sorted(expected_backend_ids))
        get_volumes_mock.assert_called()


@mock.patch(
    'waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.import_volume'
)
class VolumeImportTest(BaseVolumeTest):
    def setUp(self):
        super(VolumeImportTest, self).setUp()
        self.fixture = fixtures.OpenStackTenantFixture()
        self.url = factories.VolumeFactory.get_list_url('import_resource')
        self.client.force_authenticate(self.fixture.owner)

    def test_backend_volume_is_imported(self, import_volume_mock):
        backend_id = 'backend_id'

        def import_volume(backend_id, save, service_project_link):
            return self._generate_backend_volumes()[0]

        import_volume_mock.side_effect = import_volume

        payload = {
            'backend_id': backend_id,
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(
                self.fixture.spl
            ),
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_backend_volume_cannot_be_imported_if_it_is_registered_in_waldur(
        self, import_volume_mock
    ):
        volume = factories.VolumeFactory(service_project_link=self.fixture.spl)

        def import_volume(backend_id, save, service_project_link):
            return volume

        import_volume_mock.side_effect = import_volume
        payload = {
            'backend_id': volume.backend_id,
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(
                self.fixture.spl
            ),
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class BaseVolumeCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.settings = self.fixture.openstack_tenant_service_settings
        self.image = factories.ImageFactory(settings=self.settings)
        self.image_url = factories.ImageFactory.get_url(self.image)
        self.spl_url = factories.OpenStackTenantServiceProjectLinkFactory.get_url(
            self.fixture.spl
        )
        self.client.force_authenticate(self.fixture.owner)

    def create_volume(self, **extra):
        payload = {
            'name': 'Test volume',
            'service_project_link': self.spl_url,
            'size': 10240,
        }
        payload.update(extra)

        url = factories.VolumeFactory.get_list_url()
        return self.client.post(url, payload)


class VolumeNameCreateTest(BaseVolumeCreateTest):
    def test_image_name_populated_on_volume_creation(self):
        response = self.create_volume(image=self.image_url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['image_name'], self.image.name)

    def test_volume_image_name_populated_on_instance_creation(self):
        flavor = factories.FlavorFactory(settings=self.settings)
        flavor_url = factories.FlavorFactory.get_url(flavor)
        subnet_url = factories.SubNetFactory.get_url(self.fixture.subnet)
        url = factories.InstanceFactory.get_list_url()

        payload = {
            'name': 'test-instance',
            'image': self.image_url,
            'service_project_link': self.spl_url,
            'flavor': flavor_url,
            'system_volume_size': 20480,
            'internal_ips_set': [{'subnet': subnet_url}],
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        system_volume = response.data['volumes'][0]
        self.assertEqual(system_volume['image_name'], self.image.name)

    def test_create_instance_with_data_volumes_with_different_names(self):
        flavor = factories.FlavorFactory(settings=self.settings)
        flavor_url = factories.FlavorFactory.get_url(flavor)
        subnet_url = factories.SubNetFactory.get_url(self.fixture.subnet)
        url = factories.InstanceFactory.get_list_url()

        payload = {
            'name': 'test-instance',
            'image': self.image_url,
            'service_project_link': self.spl_url,
            'flavor': flavor_url,
            'system_volume_size': 20480,
            'internal_ips_set': [{'subnet': subnet_url}],
            'data_volumes': [
                {'size': 1024, 'type': factories.VolumeTypeFactory.get_url(),},
                {'size': 1024 * 3, 'type': factories.VolumeTypeFactory.get_url(),},
            ],
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        data_volumes_names = [
            v['name'] for v in response.data['volumes'] if not v['bootable']
        ]
        self.assertEqual(
            set(['test-instance-data-3', 'test-instance-data-2']),
            set(data_volumes_names),
        )


class VolumeTypeCreateTest(BaseVolumeCreateTest):
    def setUp(self):
        super(VolumeTypeCreateTest, self).setUp()
        self.type = factories.VolumeTypeFactory(
            settings=self.settings, backend_id='ssd'
        )
        self.type_url = factories.VolumeTypeFactory.get_url(self.type)

    def test_type_populated_on_volume_creation(self):
        response = self.create_volume(type=self.type_url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['type'], self.type_url)

    def test_volume_type_should_be_related_to_the_same_service_settings(self):
        response = self.create_volume(type=factories.VolumeTypeFactory.get_url())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('type', response.data)

    def test_when_volume_is_created_volume_type_quota_is_updated(self):
        self.create_volume(type=self.type_url, size=1024 * 10)

        key = 'gigabytes_' + self.type.backend_id
        usage = self.settings.quotas.get(name=key).usage
        self.assertEqual(usage, 10)

    def test_user_can_not_create_volume_if_resulting_quota_usage_is_greater_than_limit(
        self,
    ):
        self.settings.set_quota_usage('gigabytes_ssd', 0)
        self.settings.set_quota_limit('gigabytes_ssd', 0)

        response = self.create_volume(type=self.type_url, size=1024)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VolumeAvailabilityZoneCreateTest(BaseVolumeCreateTest):
    def test_availability_zone_should_be_related_to_the_same_service_settings(self):
        response = self.create_volume(
            availability_zone=factories.VolumeAvailabilityZoneFactory.get_url()
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_availability_zone_should_be_available(self):
        zone = self.fixture.volume_availability_zone
        zone.available = False
        zone.save()

        response = self.create_volume(
            availability_zone=factories.VolumeAvailabilityZoneFactory.get_url(zone)
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_availability_zone_name_is_validated(self):
        zone = self.fixture.volume_availability_zone

        response = self.create_volume(
            availability_zone=factories.VolumeAvailabilityZoneFactory.get_url(zone)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_openstack_tenant_settings(REQUIRE_AVAILABILITY_ZONE=True)
    def test_when_availability_zone_is_mandatory_and_exists_validation_fails(self):
        self.fixture.volume_availability_zone
        response = self.create_volume()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_openstack_tenant_settings(REQUIRE_AVAILABILITY_ZONE=True)
    def test_when_availability_zone_is_mandatory_and_does_not_exist_validation_succeeds(
        self,
    ):
        response = self.create_volume()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class VolumeRetypeTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        self.staff = self.fixture.staff
        self.volume = self.fixture.volume
        self.volume.runtime_state = 'available'
        self.volume.save()
        self.new_type = factories.VolumeTypeFactory(
            settings=self.fixture.openstack_tenant_service_settings,
        )

    def retype_volume(self, user, new_type):
        url = factories.VolumeFactory.get_url(self.volume, action='retype')
        self.client.force_authenticate(user)
        return self.client.post(
            url, {'type': factories.VolumeTypeFactory.get_url(new_type)}
        )

    @data('admin', 'manager')
    def test_user_can_resize_size_of_volume_he_has_access_to(self, user):
        response = self.retype_volume(getattr(self, user), self.new_type)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        self.volume.refresh_from_db()
        self.assertEqual(self.volume.type, self.new_type)

    def test_user_can_not_extend_volume_if_volume_operation_is_performed(self):
        self.volume.state = models.Volume.States.UPDATING
        self.volume.save()

        response = self.retype_volume(self.admin, self.new_type)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_user_can_not_extend_volume_if_volume_is_in_erred_state(self):
        self.volume.state = models.Instance.States.ERRED
        self.volume.save()

        response = self.retype_volume(self.admin, self.new_type)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_when_volume_is_retyped_volume_type_quota_is_updated(self):
        # Arrange
        scope = self.volume.service_project_link.service.settings
        old_type_key = 'gigabytes_' + self.volume.type.backend_id
        new_type_key = 'gigabytes_' + self.new_type.backend_id
        scope.set_quota_usage(old_type_key, self.volume.size / 1024)

        # Act
        self.retype_volume(self.admin, self.new_type)

        # Assert
        self.assertEqual(0, scope.quotas.get(name=old_type_key).usage)
        self.assertEqual(
            self.volume.size / 1024, scope.quotas.get(name=new_type_key).usage
        )

    def test_when_volume_is_extended_volume_type_quota_for_shared_tenant_is_updated(
        self,
    ):
        # Arrange
        scope = self.volume.service_project_link.service.settings.scope
        old_type_key = 'gigabytes_' + self.volume.type.backend_id
        new_type_key = 'gigabytes_' + self.new_type.backend_id
        scope.set_quota_usage(old_type_key, self.volume.size / 1024)

        # Act
        self.retype_volume(self.admin, self.new_type)

        # Assert
        self.assertEqual(0, scope.quotas.get(name=old_type_key).usage)
        self.assertEqual(
            self.volume.size / 1024, scope.quotas.get(name=new_type_key).usage
        )


class VolumeFilterTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.OpenStackTenantFixture()
        self.url = factories.VolumeFactory.get_list_url()
        self.instance = self.fixture.instance
        self.volume = self.fixture.volume
        self.client.force_authenticate(user=self.fixture.owner)

    def test_filter_volumes_by_valid_instance_uuid(self):
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'available'
        self.volume.save()

        volume1 = factories.VolumeFactory(
            service_project_link=self.fixture.spl,
            state=models.Volume.States.OK,
            runtime_state='available',
            type=self.fixture.volume_type,
            availability_zone=self.fixture.volume_availability_zone,
        )

        new_fixture = fixtures.OpenStackTenantFixture()
        volume2 = new_fixture.volume

        response = self.client.get(
            self.url, {'attach_instance_uuid': self.instance.uuid.hex}
        )
        volume_names = [volume['name'] for volume in response.data]
        self.assertEqual(response.status_code, status.HTTP_200_OK, data)
        self.assertEqual(len(volume_names), 2)
        self.assertIn(self.volume.name, volume_names)
        self.assertIn(volume1.name, volume_names)
        self.assertNotIn(volume2.name, volume_names)

    def test_filter_volumes_by_invalid_instance_uuid(self):
        response = self.client.get(self.url, {'attach_instance_uuid': 'invalid'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
