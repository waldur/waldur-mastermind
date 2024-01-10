from unittest.mock import patch

from ddt import data, ddt
from rest_framework import status, test

from waldur_openstack.openstack import models
from waldur_openstack.openstack.tests import factories, fixtures


class BaseServerGroupTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


@ddt
class ServerGroupCreateTest(BaseServerGroupTest):
    def setUp(self):
        super().setUp()
        self.valid_data = {"name": "Server group name", "policy": "affinity"}
        self.url = factories.TenantFactory.get_url(
            self.fixture.tenant, "create_server_group"
        )

    @data("staff", "owner", "admin", "manager")
    def test_user_with_access_can_create_server_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.ServerGroup.objects.count(), 1)

    def test_server_group_name_should_be_unique(self):
        self.client.force_authenticate(self.fixture.admin)
        payload = self.valid_data
        payload["name"] = self.fixture.server_group.name
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_server_group_creation_starts_sync_task(self):
        self.client.force_authenticate(self.fixture.admin)

        with patch(
            "waldur_openstack.openstack.executors.ServerGroupCreateExecutor.execute"
        ) as mocked_execute:
            response = self.client.post(self.url, data=self.valid_data)

            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, response.data
            )
            server_group = models.ServerGroup.objects.get(name=self.valid_data["name"])

            mocked_execute.assert_called_once_with(server_group)


@ddt
class ServerGroupDeleteTest(BaseServerGroupTest):
    def setUp(self):
        super().setUp()
        self.server_group = factories.ServerGroupFactory(
            service_settings=self.fixture.openstack_service_settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=models.ServerGroup.States.OK,
        )
        self.url = factories.ServerGroupFactory.get_url(self.server_group)

    @data("admin", "manager", "staff", "owner")
    def test_project_administrator_can_delete_server_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        with patch(
            "waldur_openstack.openstack.executors.ServerGroupDeleteExecutor.execute"
        ) as mocked_execute:
            response = self.client.delete(self.url)
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

            mocked_execute.assert_called_once_with(
                self.server_group, force=False, is_async=True
            )

    def test_server_group_can_be_deleted_from_erred_state(self):
        self.server_group.state = models.ServerGroup.States.ERRED
        self.server_group.save()

        self.client.force_authenticate(self.fixture.admin)
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
