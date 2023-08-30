from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import add_permission
from waldur_core.structure.tests.factories import UserFactory

ROLE_ENDPOINT = '/api/roles/'


class RoleTest(test.APITransactionTestCase):
    def test_get_role(self):
        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING)
        response = self.client.get(ROLE_ENDPOINT)
        self.assertEqual(
            list(response.data[0]['permissions']), [PermissionEnum.UPDATE_OFFERING]
        )

    def test_staff_can_create_role(self):
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.post(
            ROLE_ENDPOINT,
            {
                'name': RoleEnum.CUSTOMER_OWNER,
                'permissions': [PermissionEnum.UPDATE_OFFERING.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_non_staff_can_not_create_create_role(self):
        user = UserFactory(is_staff=False)
        self.client.force_login(user)
        response = self.client.post(
            ROLE_ENDPOINT,
            {
                'name': RoleEnum.CUSTOMER_OWNER,
                'permissions': [PermissionEnum.UPDATE_OFFERING.value],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_update_role(self):
        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role_uuid = response.data[0]['uuid']
        response = self.client.put(
            f'{ROLE_ENDPOINT}{role_uuid}/',
            {
                'name': RoleEnum.CUSTOMER_OWNER,
                'permissions': [
                    PermissionEnum.UPDATE_OFFERING.value,
                    PermissionEnum.APPROVE_ORDER.value,
                ],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data['permissions'],
            [PermissionEnum.UPDATE_OFFERING, PermissionEnum.APPROVE_ORDER],
        )

    def test_staff_can_not_update_system_role_name(self):
        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role_uuid = response.data[0]['uuid']
        response = self.client.put(
            f'{ROLE_ENDPOINT}{role_uuid}/',
            {
                'name': 'new name',
                'permissions': [
                    PermissionEnum.UPDATE_OFFERING.value,
                    PermissionEnum.APPROVE_ORDER.value,
                ],
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_can_not_destroy_system_role(self):
        add_permission(RoleEnum.CUSTOMER_OWNER, PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role_uuid = response.data[0]['uuid']
        response = self.client.delete(f'{ROLE_ENDPOINT}{role_uuid}/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
