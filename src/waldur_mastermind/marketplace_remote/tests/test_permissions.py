import uuid
from datetime import UTC, datetime, timedelta
from unittest import skip

import respx
from django.test import override_settings
from rest_framework import test

from waldur_auth_social.const import ProviderChoices
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace.enums import REMOTE_OFFERING
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_remote.tests.dns_utils import (
    create_selective_dns_mock,
)
from waldur_mastermind.marketplace_remote.tests.utils import (
    get_query_params,
    get_request_data,
)
from waldur_mastermind.marketplace_remote.utils import get_project_backend_id

REMOTE_USER_UUID = uuid.uuid4().hex
REMOTE_PROJECT_UUID = uuid.uuid4().hex
REMOTE_CUSTOMER_UUID = uuid.uuid4().hex


@override_settings(
    WALDUR_AUTH_SOCIAL={"ENABLE_EDUTEAMS_SYNC": True},
    task_always_eager=True,
    task_eager_propagates=True,
)
class RemoteProjectPermissionsTestCase(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.new_user = UserFactory(registration_method=ProviderChoices.EDUTEAMS)

        resource = self.fixture.resource
        resource.set_state_ok()
        resource.save()
        self.resource = resource

        offering = self.fixture.offering
        self.api_url = "http://offerings.example.com"
        offering.backend_id = "ABC"
        offering.secret_options = {
            "api_url": self.api_url,
            "token": "AAABBBCCC",
            "customer_uuid": REMOTE_CUSTOMER_UUID,
        }
        offering.type = REMOTE_OFFERING
        offering.save()
        self.offering = offering

        self.customer = self.fixture.customer

    def mock_remote_eduteams(self):
        return respx.post(
            f"{self.api_url}/api/remote-eduteams/",
        ).respond(
            200,
            json={"uuid": REMOTE_USER_UUID},
        )

    def mock_list_projects(self):
        return respx.get(
            f"{self.api_url}/api/projects/",
        ).respond(
            200,
            json=[{"uuid": REMOTE_PROJECT_UUID}],
        )

    def mock_list_users(self, json):
        return respx.get(
            f"{self.api_url}/api/projects/{REMOTE_PROJECT_UUID}/list_users/"
        ).respond(200, json=json)

    def mock_add_user(self):
        return respx.post(
            f"{self.api_url}/api/projects/{REMOTE_PROJECT_UUID}/add_user/"
        ).respond(201, json={"expiration_time": None})

    def mock_update_user(self):
        return respx.post(
            f"{self.api_url}/api/projects/{REMOTE_PROJECT_UUID}/update_user/"
        ).respond(200, json={"expiration_time": None})

    def mock_delete_user(self):
        return respx.post(
            f"{self.api_url}/api/projects/{REMOTE_PROJECT_UUID}/delete_user/"
        ).respond(200)

    def tearDown(self):
        respx.stop()
        self.dns_patcher.stop()
        super().tearDown()

    def test_create_remote_permission(self):
        mock_eduteams = self.mock_remote_eduteams()
        mock_list_projects = self.mock_list_projects()
        mock_list_users = self.mock_list_users([])

        add_user_mock = self.mock_add_user()
        self.project.add_user(user=self.new_user, role=ProjectRole.ADMIN)

        self.assertDictEqual(
            get_request_data(mock_eduteams), {"cuid": self.new_user.username}
        )

        self.assertDictEqual(
            get_query_params(mock_list_projects),
            {
                "backend_id": get_project_backend_id(self.project),
            },
        )

        self.assertDictEqual(
            get_query_params(mock_list_users),
            {
                "user": REMOTE_USER_UUID,
                "role": RoleEnum.PROJECT_ADMIN.value,
            },
        )

        self.assertDictEqual(
            get_request_data(add_user_mock),
            {
                "user": REMOTE_USER_UUID,
                "role": RoleEnum.PROJECT_ADMIN.value,
                "expiration_time": None,
            },
        )

    def test_create_remote_permission_with_expiration_time(self):
        mock_eduteams = self.mock_remote_eduteams()
        mock_list_projects = self.mock_list_projects()
        mock_list_users = self.mock_list_users([])

        expiration_time = datetime.now(UTC) + timedelta(days=1)
        add_user_mock = self.mock_add_user()
        self.project.add_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
            expiration_time=expiration_time,
        )
        self.assertEqual(
            get_request_data(mock_eduteams), {"cuid": self.new_user.username}
        )
        self.assertEqual(
            mock_list_projects.calls.last.request.url.params["backend_id"],
            get_project_backend_id(self.project),
        )
        self.assertEqual(
            mock_list_users.calls.last.request.url.params["user"], REMOTE_USER_UUID
        )
        self.assertEqual(
            mock_list_users.calls.last.request.url.params["role"],
            RoleEnum.PROJECT_ADMIN.value,
        )
        self.assertTrue(add_user_mock.called)
        self.assertEqual(
            get_request_data(add_user_mock),
            {
                "user": REMOTE_USER_UUID,
                "role": RoleEnum.PROJECT_ADMIN.value,
                "expiration_time": expiration_time.isoformat(),
            },
        )

    def test_update_remote_permission(self):
        self.mock_remote_eduteams()
        self.mock_list_projects()

        old_expiration_time = datetime.now(UTC) + timedelta(days=1)
        new_expiration_time = datetime.now(UTC) + timedelta(days=2)
        self.mock_list_users([{"expiration_time": old_expiration_time.isoformat()}])
        update_user_mock = self.mock_update_user()

        permission = self.project.add_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
            expiration_time=old_expiration_time,
        )
        permission.set_expiration_time(new_expiration_time)

        self.assertTrue(update_user_mock.called)
        self.assertEqual(
            get_request_data(update_user_mock),
            {
                "user": REMOTE_USER_UUID,
                "role": RoleEnum.PROJECT_ADMIN.value,
                "expiration_time": new_expiration_time.isoformat(),
            },
        )

    def test_delete_remote_permission(self):
        self.mock_remote_eduteams()
        self.mock_list_projects()
        self.mock_list_users([{"expiration_time": None}])
        delete_user_mock = self.mock_delete_user()

        self.project.add_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
        )
        self.project.remove_user(
            user=self.new_user,
            role=ProjectRole.ADMIN,
        )
        self.assertTrue(delete_user_mock.called)
        self.assertEqual(
            get_request_data(delete_user_mock),
            {
                "user": REMOTE_USER_UUID,
                "role": RoleEnum.PROJECT_ADMIN.value,
            },
        )

    def test_organization_owner_is_not_synced(self):
        # Only project-level permissions are propagated to remote Waldur;
        # granting an organization owner must not trigger any remote calls.
        mock_eduteams = self.mock_remote_eduteams()
        mock_list_projects = self.mock_list_projects()
        add_user_mock = self.mock_add_user()

        self.customer.add_user(user=self.new_user, role=CustomerRole.OWNER)

        self.assertFalse(mock_eduteams.called)
        self.assertFalse(mock_list_projects.called)
        self.assertFalse(add_user_mock.called)

    @skip("Unstable in CI/CD")
    def test_sync_resource_team(self):
        self.fixture.manager
        stale_user_uuid = uuid.uuid4().hex
        self.mock_list_users(
            [
                {
                    "uuid": stale_user_uuid,
                    "role": RoleEnum.PROJECT_ADMIN,
                    "username": "stale_username_00",
                }
            ]
        )
        self.mock_list_users([{"role_name": RoleEnum.PROJECT_ADMIN}])
        self.mock_list_users([])

        self.client.force_login(self.fixture.staff)
        response = self.client.post(
            f"http://testserver/api/remote-waldur-api/sync_resource_project_permissions/{self.resource.uuid.hex}/"
        )

        self.assertEqual(200, response.status_code)

        delete_user_mock = self.mock_delete_user()
        self.assertTrue(delete_user_mock.called)
        self.assertEqual(
            get_request_data(delete_user_mock),
            {
                "user": stale_user_uuid,
                "role": RoleEnum.PROJECT_ADMIN.value,
            },
        )

        add_user_mock = self.mock_add_user()
        self.assertTrue(add_user_mock.called)
        self.assertEqual(
            get_request_data(add_user_mock),
            {
                "user": REMOTE_USER_UUID,
                "role": RoleEnum.PROJECT_MANAGER.value,
                "expiration_time": None,
            },
        )
