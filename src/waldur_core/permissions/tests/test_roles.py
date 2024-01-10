from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace.tests import fixtures

ROLE_ENDPOINT = "/api/roles/"


class RoleTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.project = self.fixture.project

    def test_get_role(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        response = self.client.get(ROLE_ENDPOINT)
        self.assertEqual(
            list(response.data[0]["permissions"]), [PermissionEnum.UPDATE_OFFERING]
        )

    def test_staff_can_create_role(self):
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.post(
            ROLE_ENDPOINT,
            {
                "name": RoleEnum.CUSTOMER_OWNER,
                "content_type": "customer",
                "permissions": [PermissionEnum.UPDATE_OFFERING.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_non_staff_can_not_create_create_role(self):
        user = UserFactory(is_staff=False)
        self.client.force_login(user)
        response = self.client.post(
            ROLE_ENDPOINT,
            {
                "name": RoleEnum.CUSTOMER_OWNER,
                "content_type": "customer",
                "permissions": [PermissionEnum.UPDATE_OFFERING.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_update_role(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role_uuid = response.data[0]["uuid"]
        response = self.client.put(
            f"{ROLE_ENDPOINT}{role_uuid}/",
            {
                "name": RoleEnum.CUSTOMER_OWNER,
                "content_type": "customer",
                "permissions": [
                    PermissionEnum.UPDATE_OFFERING.value,
                    PermissionEnum.APPROVE_ORDER.value,
                ],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["permissions"],
            [PermissionEnum.UPDATE_OFFERING, PermissionEnum.APPROVE_ORDER],
        )

    def test_staff_can_not_update_system_role_name(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role_uuid = response.data[0]["uuid"]
        response = self.client.put(
            f"{ROLE_ENDPOINT}{role_uuid}/",
            {
                "name": "new name",
                "permissions": [
                    PermissionEnum.UPDATE_OFFERING.value,
                    PermissionEnum.APPROVE_ORDER.value,
                ],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_can_not_destroy_system_role(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role_uuid = response.data[0]["uuid"]
        response = self.client.delete(f"{ROLE_ENDPOINT}{role_uuid}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_users_project_has_user(self):
        self.user = self.fixture.admin
        self.client.force_authenticate(self.user)
        url = f"http://testserver/api/projects/{self.project.uuid}/list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_list_users_with_no_user(self):
        self.user = self.fixture.staff
        self.client.force_authenticate(self.user)
        url = f"http://testserver/api/projects/{self.project.uuid}/list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)
