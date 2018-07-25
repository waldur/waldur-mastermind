from ddt import ddt, data
from django.conf import settings
from rest_framework import test, status
import mock

from waldur_core.structure.models import ProjectRole
from waldur_core.structure.tests import factories as structure_factories

from . import factories, fixtures
from .. import models


class VolumeDeleteTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.volume = self.fixture.volume
        self.spl = self.fixture.spl

    def test_spl_quota_updated_by_signal_handler_when_volume_is_removed(self):
        self.volume.delete()
        Quotas = models.OpenStackTenantServiceProjectLink.Quotas
        self.assertEqual(self.spl.quotas.get(name=Quotas.storage).usage, 0)


@ddt
class VolumeExtendTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.admin = structure_factories.UserFactory()
        self.manager = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.admined_volume = factories.VolumeFactory(state=models.Volume.States.OK)

        admined_project = self.admined_volume.service_project_link.project
        admined_project.add_user(self.admin, ProjectRole.ADMINISTRATOR)
        admined_project.add_user(self.manager, ProjectRole.MANAGER)

    @data('admin', 'manager')
    def test_user_can_resize_size_of_volume_he_has_access_to(self, user):
        self.client.force_authenticate(getattr(self, user))
        new_size = self.admined_volume.size + 1024

        url = factories.VolumeFactory.get_url(self.admined_volume, action='extend')
        response = self.client.post(url, {'disk_size': new_size})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        self.admined_volume.refresh_from_db()
        self.assertEqual(self.admined_volume.size, new_size)

    def test_user_can_not_extend_volume_if_resulting_quota_usage_is_greater_than_limit(self):
        self.client.force_authenticate(user=self.admin)
        settings = self.admined_volume.service_project_link.service.settings
        settings.set_quota_usage('storage', self.admined_volume.size)
        settings.set_quota_limit('storage', self.admined_volume.size + 512)

        self.assert_extend_request_returns_bad_request()

    def test_user_can_not_extend_volume_if_quota_usage_becomes_greater_than_limit_in_service_project_link(self):
        self.client.force_authenticate(user=self.admin)
        self.admined_volume.service_project_link.set_quota_usage('storage', self.admined_volume.size)
        self.admined_volume.service_project_link.set_quota_limit('storage', self.admined_volume.size + 512)

        self.assert_extend_request_returns_bad_request()

    def assert_extend_request_returns_bad_request(self, extend_by=1024):
        new_size = self.admined_volume.size + extend_by
        url = factories.VolumeFactory.get_url(self.admined_volume, action='extend')

        response = self.client.post(url, {'disk_size': new_size})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_user_can_not_extend_volume_if_volume_operation_is_performed(self):
        self.client.force_authenticate(user=self.admin)
        self.admined_volume.state = models.Volume.States.UPDATING
        self.admined_volume.save()

        new_size = self.admined_volume.size + 1024
        url = factories.VolumeFactory.get_url(self.admined_volume, action='extend')

        response = self.client.post(url, {'disk_size': new_size})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)

    def test_user_can_not_extend_volume_if_volume_is_in_erred_state(self):
        self.client.force_authenticate(user=self.admin)
        self.admined_volume.state = models.Instance.States.ERRED
        self.admined_volume.save()

        new_size = self.admined_volume.size + 1024
        url = factories.VolumeFactory.get_url(self.admined_volume, action='extend')

        response = self.client.post(url, {'disk_size': new_size})
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT, response.data)


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


class VolumeSnapshotTestCase(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.volume = self.fixture.volume
        self.url = factories.VolumeFactory.get_url(self.volume, action='snapshot')
        self.volume.state = models.Volume.States.OK
        self.volume.runtime_state = 'available'
        self.volume.save()

        self.client.force_authenticate(self.fixture.owner)

    def test_user_can_create_volume_snapshot(self):
        payload = {'name': '%s snapshot' % self.volume.name}

        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_snapshot_metadata_is_populated(self):
        self.client.force_authenticate(self.fixture.owner)
        payload = {'name': '%s snapshot' % self.volume.name}

        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        snapshot = models.Snapshot.objects.get(uuid=response.data['uuid'])
        self.assertIn('source_volume_name', snapshot.metadata)
        self.assertEqual(snapshot.metadata['source_volume_name'], self.volume.name)
        self.assertEqual(snapshot.metadata['source_volume_description'], self.volume.description)
        self.assertEqual(snapshot.metadata['source_volume_image_metadata'], self.volume.image_metadata)


@ddt
class VolumeCreateSnapshotScheduleTest(test.APITransactionTestCase):
    action_name = 'create_snapshot_schedule'

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.url = factories.VolumeFactory.get_url(self.fixture.volume, self.action_name)
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
        self.assertEqual(response.data['retention_time'], self.snapshot_schedule_data['retention_time'])
        self.assertEqual(
            response.data['maximal_number_of_resources'], self.snapshot_schedule_data['maximal_number_of_resources'])
        self.assertEqual(response.data['schedule'], self.snapshot_schedule_data['schedule'])

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
        self.assertIn('schedule', response.content)

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

    @mock.patch('waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.get_volumes_for_import')
    def test_importable_volumes_are_returned(self, get_volumes_mock):
        backend_volumes = self._generate_backend_volumes()
        get_volumes_mock.return_value = backend_volumes
        data = {'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl)}

        response = self.client.get(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEquals(len(response.data), len(backend_volumes))
        returned_backend_ids = [item['backend_id'] for item in response.data]
        expected_backend_ids = [item.backend_id for item in backend_volumes]
        self.assertItemsEqual(returned_backend_ids, expected_backend_ids)
        get_volumes_mock.assert_called()


class VolumeImportTest(BaseVolumeTest):

    def setUp(self):
        super(VolumeImportTest, self).setUp()
        self.fixture = fixtures.OpenStackTenantFixture()
        self.url = factories.VolumeFactory.get_list_url('import_resource')
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.import_volume')
    def test_backend_volume_is_imported(self, import_volume_mock):
        backend_id = 'backend_id'

        def import_volume(backend_id, save, service_project_link):
            return self._generate_backend_volumes()[0]

        import_volume_mock.side_effect = import_volume

        payload = {
            'backend_id': backend_id,
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl),
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @mock.patch('waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.import_volume')
    def test_backend_volume_cannot_be_imported_if_it_is_registered_in_waldur(self, import_volume_mock):
        volume = factories.VolumeFactory(service_project_link=self.fixture.spl)

        def import_volume(backend_id, save, service_project_link):
            return volume

        import_volume_mock.side_effect = import_volume
        payload = {
            'backend_id': volume.backend_id,
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl),
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VolumeCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()
        self.settings = self.fixture.openstack_tenant_service_settings
        self.image = factories.ImageFactory(settings=self.settings)
        self.image_url = factories.ImageFactory.get_url(self.image)
        self.spl_url = factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl)
        self.client.force_authenticate(self.fixture.owner)

    def test_image_name_populated_on_volume_creation(self):
        url = factories.VolumeFactory.get_list_url()
        payload = {
            'name': 'Test volume',
            'image': self.image_url,
            'service_project_link': self.spl_url,
            'size': 10240
        }

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['image_name'], self.image.name)

    def test_volume_image_name_populated_on_instance_creation(self):
        flavor = factories.FlavorFactory(settings=self.settings)
        flavor_url = factories.FlavorFactory.get_url(flavor)
        subnet_url = factories.SubNetFactory.get_url(self.fixture.subnet)
        url = factories.InstanceFactory.get_list_url()

        payload = {
            'name': 'Test instance',
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
