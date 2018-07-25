from ddt import ddt, data
from rest_framework import test, status
import mock

from waldur_openstack.openstack_tenant import models

from . import factories, fixtures


@ddt
class SnapshotRestoreTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()

    def _make_restore_request(self):
        url = factories.SnapshotFactory.get_url(snapshot=self.fixture.snapshot, action='restore')
        request_data = {
            'name': '/dev/sdb1',
        }

        response = self.client.post(url, request_data)
        return response

    @data('global_support', 'customer_support', 'project_support')
    def test_user_cannot_restore_snapshot_if_he_has_not_admin_access(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self._make_restore_request()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'owner', 'admin', 'manager')
    def test_user_can_restore_snapshot_only_if_he_has_admin_access(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self._make_restore_request()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('user')
    def test_user_cannot_restore_snapshot_if_he_has_no_project_level_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self._make_restore_request()

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_snapshot_restore_creates_volume(self):
        self.client.force_authenticate(self.fixture.owner)

        response = self._make_restore_request()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.SnapshotRestoration.objects.count(), 1)
        restoration = models.SnapshotRestoration.objects.first()
        restored_volume = models.Volume.objects.exclude(pk=self.fixture.snapshot.source_volume.pk).first()
        self.assertEqual(self.fixture.snapshot, restoration.snapshot)
        self.assertEqual(restored_volume, restoration.volume)

    def test_user_is_able_to_specify_a_name_of_the_restored_volume(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.SnapshotFactory.get_url(snapshot=self.fixture.snapshot, action='restore')

        expected_name = 'C:/ Drive'
        request_data = {
            'name': expected_name,
        }

        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created_volume = models.SnapshotRestoration.objects.first().volume
        self.assertIn(expected_name, created_volume.name)
        self.assertEqual(response.data['uuid'], created_volume.uuid.hex)
        self.assertEqual(response.data['name'], created_volume.name)

    def test_user_is_able_to_specify_a_description_of_the_restored_volume(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.SnapshotFactory.get_url(snapshot=self.fixture.snapshot, action='restore')

        expected_description = 'Restored after blue screen.'
        request_data = {
            'name': '/dev/sdb2',
            'description': expected_description,
        }

        response = self.client.post(url, request_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created_volume = models.SnapshotRestoration.objects.first().volume
        self.assertIn(expected_description, created_volume.description)
        self.assertEqual(response.data['uuid'], created_volume.uuid.hex)
        self.assertEqual(response.data['description'], created_volume.description)

    def test_restore_is_not_available_if_snapshot_is_not_in_OK_state(self):
        self.client.force_authenticate(self.fixture.owner)
        snapshot = factories.SnapshotFactory(
            service_project_link=self.fixture.spl,
            source_volume=self.fixture.volume,
            state=models.Snapshot.States.ERRED)
        url = factories.SnapshotFactory.get_url(snapshot=snapshot, action='restore')

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_restore_cannot_be_made_if_volume_exceeds_quota(self):
        self.client.force_authenticate(self.fixture.owner)
        quota = self.fixture.openstack_tenant_service_settings.quotas.get(name='volumes')
        quota.limit = quota.usage
        quota.save()
        snapshot = self.fixture.snapshot
        expected_volumes_amount = models.Volume.objects.count()

        url = factories.SnapshotFactory.get_url(snapshot=snapshot, action='restore')
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.state, snapshot.States.OK)
        self.assertEqual(expected_volumes_amount, models.Volume.objects.count())

    def test_restore_cannot_be_made_if_service_project_link_storage_quota_exceeds_its_limit(self):
        self.fixture.snapshot
        self.fixture.spl.set_quota_limit('storage', 0)
        self.client.force_authenticate(self.fixture.owner)

        response = self._make_restore_request()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class SnapshotRetrieveTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()

    @data('staff', 'owner', 'admin', 'manager', 'global_support')
    def test_a_list_of_restored_volumes_are_displayed_if_user_has_project_level_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        snapshot_restoration = factories.SnapshotRestorationFactory(snapshot=self.fixture.snapshot)
        url = factories.SnapshotFactory.get_url(snapshot=snapshot_restoration.snapshot)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['uuid'], snapshot_restoration.snapshot.uuid.hex)
        self.assertIn('restorations', response.data)
        self.assertEquals(len(response.data['restorations']), 1)

    @data('user')
    def test_user_cannot_see_snapshot_restoration_if_has_no_project_level_permissions(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))
        self.fixture.snapshot

        url = factories.SnapshotFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class BaseSnapshotImportTest(test.APITransactionTestCase):

    def _generate_backend_snapshots(self, count=1):
        snapshots = []
        for i in range(count):
            snapshot = factories.SnapshotFactory()
            snapshot.delete()
            snapshots.append(snapshot)

        return snapshots


class SnapshotImportableResourcesTest(BaseSnapshotImportTest):

    def setUp(self):
        super(SnapshotImportableResourcesTest, self).setUp()
        self.url = factories.SnapshotFactory.get_list_url('importable_resources')
        self.fixture = fixtures.OpenStackTenantFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.get_snapshots_for_import')
    def test_importable_volumes_are_returned(self, get_volumes_mock):
        backend_snapshots = self._generate_backend_snapshots()
        get_volumes_mock.return_value = backend_snapshots
        data = {'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl)}

        response = self.client.get(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEquals(len(response.data), len(backend_snapshots))
        returned_backend_ids = [item['backend_id'] for item in response.data]
        expected_backend_ids = [item.backend_id for item in backend_snapshots]
        self.assertItemsEqual(returned_backend_ids, expected_backend_ids)
        get_volumes_mock.assert_called()


class SnapshotImportResourceTest(BaseSnapshotImportTest):

    def setUp(self):
        super(SnapshotImportResourceTest, self).setUp()
        self.url = factories.SnapshotFactory.get_list_url('import_resource')
        self.fixture = fixtures.OpenStackTenantFixture()
        self.client.force_authenticate(self.fixture.owner)

    @mock.patch('waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.import_snapshot')
    def test_backend_volume_is_imported(self, import_snapshot_mock):
        backend_id = 'backend_id'

        def import_snapshot(backend_id, save, service_project_link):
            return self._generate_backend_snapshots()[0]

        import_snapshot_mock.side_effect = import_snapshot

        payload = {
            'backend_id': backend_id,
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl),
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @mock.patch('waldur_openstack.openstack_tenant.backend.OpenStackTenantBackend.import_snapshot')
    def test_backend_volume_cannot_be_imported_if_it_is_registered_in_waldur(self, import_snapshot_mock):
        snapshot = factories.SnapshotFactory(service_project_link=self.fixture.spl)

        def import_snapshot(backend_id, save, service_project_link):
            return snapshot

        import_snapshot_mock.side_effect = import_snapshot
        payload = {
            'backend_id': snapshot.backend_id,
            'service_project_link': factories.OpenStackTenantServiceProjectLinkFactory.get_url(self.fixture.spl),
        }

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
