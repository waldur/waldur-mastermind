from unittest import mock
from rest_framework import test

from waldur_core.structure.models import ProjectRole

from . import fixtures
from .. import models, utils


@mock.patch('waldur_rancher.utils.RancherBackend')
class UserSyncTest(test.APITransactionTestCase):
    def setUp(self):
        super(UserSyncTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture.admin
        self.fixture.manager
        self.fixture.owner

    def test_create_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().create_user.call_count, 3)
        self.assertEqual(models.RancherUser.objects.all().count(), 3)

    def test_delete_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.fixture.project.remove_user(self.fixture.admin)
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().block_user.call_count, 1)

    def test_update_user(self, mock_backend_class):
        utils.SyncUser.run()
        self.fixture.project.add_user(self.fixture.admin, ProjectRole.MANAGER)
        utils.SyncUser.run()
        self.assertEqual(mock_backend_class().delete_cluster_role.call_count, 1)
        self.assertEqual(mock_backend_class().create_cluster_role.call_count, 4)
