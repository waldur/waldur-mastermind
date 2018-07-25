from ddt import ddt, data

from rest_framework import status
from rest_framework import test

from waldur_openstack.openstack_tenant import models

from . import factories, fixtures


class BaseSnapshotScheduleTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()


class SnapshotScheduleActivateTest(BaseSnapshotScheduleTest):

    def setUp(self):
        super(SnapshotScheduleActivateTest, self).setUp()
        self.url = factories.SnapshotScheduleFactory.get_url(self.fixture.snapshot_schedule, 'activate')
        self.client.force_authenticate(self.fixture.owner)

    def test_snapshot_schedule_do_not_start_activation_of_active_schedule(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_snapshot_schedule_is_activated(self):
        snapshot_schedule = self.fixture.snapshot_schedule
        snapshot_schedule.is_active = False
        snapshot_schedule.save()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(models.SnapshotSchedule.objects.get(pk=snapshot_schedule.pk).is_active)


class SnapshotScheduleDeactivateTest(BaseSnapshotScheduleTest):

    def setUp(self):
        super(SnapshotScheduleDeactivateTest, self).setUp()
        self.url = factories.SnapshotScheduleFactory.get_url(self.fixture.snapshot_schedule, 'deactivate')
        self.client.force_authenticate(self.fixture.owner)

    def test_snapshot_schedule_do_not_start_deactivation_of_not_active_schedule(self):
        snapshot = self.fixture.snapshot_schedule
        snapshot.is_active = False
        snapshot.save()
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_snapshot_schedule_is_deactivated(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(models.SnapshotSchedule.objects.get(pk=self.fixture.snapshot_schedule.pk).is_active)


@ddt
class SnapshotScheduleRetrieveTest(BaseSnapshotScheduleTest):

    def setUp(self):
        super(SnapshotScheduleRetrieveTest, self).setUp()
        self.url = factories.SnapshotScheduleFactory.get_list_url()

    @data('owner', 'global_support', 'admin', 'manager', 'staff')
    def test_user_can_see_snapshots_if_he_has_permissions(self, user):
        self.fixture.snapshot_schedule
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.fixture.snapshot_schedule.uuid.hex)

    @data('user')
    def test_user_can_not_see_snapshots_if_he_has_no_project_level_permissions(self, user):
        self.fixture.snapshot_schedule
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class SnapshotScheduleDeleteTest(BaseSnapshotScheduleTest):

    def setUp(self):
        super(SnapshotScheduleDeleteTest, self).setUp()
        self.url = factories.SnapshotScheduleFactory.get_url(self.fixture.snapshot_schedule)

    @data('owner', 'admin', 'staff')
    def test_user_can_delete_snapshot(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.SnapshotSchedule.objects.filter(pk=self.fixture.snapshot_schedule.pk).exists())
