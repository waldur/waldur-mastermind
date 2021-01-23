from unittest import mock

from django.core import mail
from rest_framework import test

from waldur_core.structure.models import ProjectRole
from waldur_rancher import enums, models, tasks, utils
from waldur_rancher.tests import factories, fixtures
from waldur_rancher.tests.base import override_rancher_settings


class UserSyncTest(test.APITransactionTestCase):
    def setUp(self):
        super(UserSyncTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture.admin
        self.fixture.manager
        self.fixture.owner

    @mock.patch('waldur_rancher.utils.RancherBackend')
    def test_create_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().create_user.call_count, 3)
        self.assertEqual(models.RancherUser.objects.all().count(), 3)

    @mock.patch('waldur_rancher.utils.RancherBackend')
    @override_rancher_settings(DISABLE_AUTOMANAGEMENT_OF_USERS=True)
    def test_disable_users_automanagement(self, mock_backend_class):
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().create_user.call_count, 0)
        self.assertEqual(models.RancherUser.objects.all().count(), 0)

    @mock.patch('waldur_rancher.utils.RancherBackend')
    def test_delete_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.fixture.project.remove_user(self.fixture.admin)
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().block_user.call_count, 1)

    @mock.patch('waldur_rancher.utils.RancherBackend')
    def test_update_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.fixture.project.add_user(self.fixture.admin, ProjectRole.MANAGER)
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().delete_cluster_role.call_count, 1)
        self.assertEqual(mock_backend_class().create_cluster_role.call_count, 4)

    @mock.patch('waldur_rancher.utils.RancherBackend.client')
    @mock.patch('waldur_rancher.handlers.tasks')
    def test_notification(self, mock_tests, mock_client):
        mock_client.create_user.return_value = {'id': 'ID'}
        mock_client.create_cluster_role.return_value = {'id': 'ID'}
        utils.SyncUser.run()
        self.assertEqual(models.RancherUser.objects.all().count(), 3)
        self.assertEqual(mock_tests.notify_create_user.delay.call_count, 3)

    def test_notification_message(self):
        rancher_user = factories.RancherUserFactory()
        password = 'password'
        url = 'http//example.com'
        tasks.notify_create_user(rancher_user.id, password, url)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [rancher_user.user.email])
        self.assertTrue(url in mail.outbox[0].body)

    @mock.patch('waldur_rancher.utils.RancherBackend')
    def test_create_project_role(self, mock_backend_class):
        project = factories.ProjectFactory()
        utils.SyncUser.run()
        rancher_user = models.RancherUser.objects.first()
        rancher_user.backend_id = 'backend_id'
        rancher_user.save()

        mock_backend_class().client.get_projects_roles.return_value = [
            {
                'projectId': project.backend_id,
                'roleTemplateId': enums.ProjectRoleId.project_owner,
                'id': 'project_role_id',
                'userId': 'backend_id',
            }
        ]
        utils.SyncUser.run()

        rancher_user.refresh_from_db()
        self.assertEqual(rancher_user.rancheruserprojectlink_set.count(), 1)
        self.assertEqual(
            rancher_user.rancheruserprojectlink_set.first().backend_id,
            'project_role_id',
        )
