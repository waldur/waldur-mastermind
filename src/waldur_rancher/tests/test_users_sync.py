from unittest import mock

from rest_framework import test

from waldur_core.permissions.fixtures import ProjectRole
from waldur_rancher import models, utils
from waldur_rancher.tests import factories, fixtures
from waldur_rancher.tests.base import override_rancher_settings


class UserSyncTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture.admin
        self.fixture.manager
        self.fixture.owner
        self.fixture.cluster_owner_role
        self.fixture.cluster_member_role
        self.fixture.project_owner_role

    @mock.patch("waldur_rancher.utils.RancherBackend")
    def test_create_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().create_user.call_count, 3)
        self.assertEqual(models.RancherUser.objects.all().count(), 3)

    @mock.patch("waldur_rancher.utils.RancherBackend")
    @override_rancher_settings(DISABLE_AUTOMANAGEMENT_OF_USERS=True)
    def test_disable_users_automanagement(self, mock_backend_class):
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().create_user.call_count, 0)
        self.assertEqual(models.RancherUser.objects.all().count(), 0)

    @mock.patch("waldur_rancher.utils.RancherBackend")
    def test_delete_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.fixture.project.remove_user(self.fixture.admin)
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().block_user.call_count, 1)

    @mock.patch("waldur_rancher.utils.RancherBackend")
    def test_update_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.fixture.project.add_user(self.fixture.admin, ProjectRole.MANAGER)
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().delete_cluster_role.call_count, 1)
        self.assertEqual(mock_backend_class().create_cluster_user_role.call_count, 4)

    @mock.patch("waldur_rancher.utils.RancherBackend")
    def test_create_project_role(self, mock_backend_class):
        project = factories.ProjectFactory()
        utils.SyncUser.run()
        rancher_user = models.RancherUser.objects.first()
        rancher_user.backend_id = "backend_id"
        rancher_user.save()

        mock_backend_class().client.get_projects_roles.return_value = [
            {
                "projectId": project.backend_id,
                "roleTemplateId": "project-owner",
                "id": "project_role_id",
                "userId": "backend_id",
            }
        ]
        utils.SyncUser.run()

        rancher_user.refresh_from_db()
        self.assertEqual(rancher_user.rancheruserprojectlink_set.count(), 1)
        self.assertEqual(
            rancher_user.rancheruserprojectlink_set.first().backend_id,
            "project_role_id",
        )
