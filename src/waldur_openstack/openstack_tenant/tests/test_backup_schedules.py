from __future__ import unicode_literals

from ddt import data, ddt
from rest_framework import status
from rest_framework import test

from waldur_openstack.openstack_tenant import models

from . import factories, fixtures


class BaseBackupScheduleTest(test.APITransactionTestCase):

    def setUp(self):
        self.fixture = fixtures.OpenStackTenantFixture()


@ddt
class BackupScheduleRetrieveTest(BaseBackupScheduleTest):

    def setUp(self):
        super(BackupScheduleRetrieveTest, self).setUp()
        self.backup_schedule = self.fixture.backup_schedule
        self.url = factories.BackupScheduleFactory.get_list_url()

    @data('owner', 'manager', 'admin', 'staff', 'global_support')
    def test_user_has_access_to_backup_schedules(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['uuid'], self.backup_schedule.uuid.hex)

    @data('user')
    def test_user_can_not_see_backup_schedules_if_he_has_no_project_level_permissions(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class BackupScheduleDeleteTest(BaseBackupScheduleTest):

    def setUp(self):
        super(BackupScheduleDeleteTest, self).setUp()
        self.schedule = factories.BackupScheduleFactory(instance=self.fixture.instance)
        self.url = factories.BackupScheduleFactory.get_url(self.schedule)

    @data('owner', 'admin', 'manager', 'staff')
    def test_user_can_delete_backup_schedule(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.BackupSchedule.objects.filter(pk=self.schedule.pk).exists())

    @data('user')
    def test_user_can_not_delete_backup_schedule(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class BackupScheduleActivateTest(BaseBackupScheduleTest):

    def setUp(self):
        super(BackupScheduleActivateTest, self).setUp()
        self.client.force_authenticate(self.fixture.owner)
        self.schedule = self.fixture.backup_schedule

    def test_backup_schedule_do_not_start_activation_of_active_schedule(self):
        url = factories.BackupScheduleFactory.get_url(self.schedule, action='activate')

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_backup_schedule_can_be_activated(self):
        self.schedule.is_active = False
        self.schedule.save()
        url = factories.BackupScheduleFactory.get_url(self.schedule, action='activate')

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.schedule.refresh_from_db()
        self.assertTrue(self.schedule.is_active)

    @data('global_support')
    def test_user_cannot_activate_backup_schedule_if_he_is_not_owner(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.BackupScheduleFactory.get_url(self.schedule, action='activate')

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class BackupScheduleDeactivateTest(BaseBackupScheduleTest):

    def setUp(self):
        super(BackupScheduleDeactivateTest, self).setUp()
        self.schedule = self.fixture.backup_schedule

    def test_backup_schedule_do_not_start_deactivation_of_not_active_schedule(self):
        self.client.force_authenticate(self.fixture.owner)
        self.schedule.is_active = False
        self.schedule.save()
        url = factories.BackupScheduleFactory.get_url(self.schedule, action='deactivate')

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_backup_schedule_can_be_deactivated(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.BackupScheduleFactory.get_url(self.schedule, action='deactivate')

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.schedule.refresh_from_db()
        self.assertFalse(self.schedule.is_active)

    def test_schedule_can_be_deactivated_after_it_was_activated(self):
        self.client.force_authenticate(self.fixture.owner)
        self.schedule.is_active = False
        self.schedule.save()

        activate_url = factories.BackupScheduleFactory.get_url(self.schedule, action='activate')
        deactivate_url = factories.BackupScheduleFactory.get_url(self.schedule, action='deactivate')

        response = self.client.post(activate_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(models.BackupSchedule.objects.get(pk=self.schedule.pk).is_active)

        response = self.client.post(deactivate_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(models.BackupSchedule.objects.get(pk=self.schedule.pk).is_active)

    @data('global_support')
    def test_user_cannot_deactivate_backup_schedule_if_he_is_not_owner(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.BackupScheduleFactory.get_url(self.schedule, action='deactivate')

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
