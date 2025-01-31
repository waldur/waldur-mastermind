from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions import models
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests import factories
from waldur_core.structure.models import Project
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

    def test_ordering_of_list_users(self):
        admin = self.fixture.admin
        manager = self.fixture.manager
        self.client.force_authenticate(admin)
        url = f"http://testserver/api/projects/{self.project.uuid}/list_users/?o=full_name"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response.data[0]["user_full_name"], admin.full_name)
        self.assertEqual(response.data[1]["user_full_name"], manager.full_name)

        url = f"http://testserver/api/projects/{self.project.uuid}/list_users/?o=-full_name"
        response = self.client.get(url)
        self.assertEqual(response.data[0]["user_full_name"], manager.full_name)
        self.assertEqual(response.data[1]["user_full_name"], admin.full_name)

    def test_list_users_with_no_user(self):
        self.user = self.fixture.staff
        self.client.force_authenticate(self.user)
        url = f"http://testserver/api/projects/{self.project.uuid}/list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_staff_can_disable_role(self):
        # pre-populate the DB with a role
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role = response.data[0]
        self.assertEqual(role["is_active"], True)
        self.client.post(
            f"{ROLE_ENDPOINT}{role['uuid']}/disable/",
        )
        response = self.client.get(ROLE_ENDPOINT)
        self.assertEqual(response.data[0]["is_active"], False)

    def test_non_staff_can_not_disable_role(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=False)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role = response.data[0]
        self.assertEqual(role["is_active"], True)
        self.client.post(
            f"{ROLE_ENDPOINT}{role['uuid']}/disable/",
        )
        response = self.client.get(ROLE_ENDPOINT)
        self.assertEqual(response.data[0]["is_active"], True)

    def test_staff_can_enable_role(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role = response.data[0]
        self.assertEqual(role["is_active"], True)
        self.client.post(
            f"{ROLE_ENDPOINT}{role['uuid']}/disable/",
        )
        response = self.client.get(ROLE_ENDPOINT)
        self.assertEqual(response.data[0]["is_active"], False)
        self.client.post(
            f"{ROLE_ENDPOINT}{role['uuid']}/enable/",
        )
        response = self.client.get(ROLE_ENDPOINT)
        self.assertEqual(response.data[0]["is_active"], True)

    def test_non_staff_can_not_enable_role(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        user = UserFactory(is_staff=False)
        self.client.force_login(user)
        response = self.client.get(ROLE_ENDPOINT)
        role = response.data[0]
        action_response = self.client.post(
            f"{ROLE_ENDPOINT}{role['uuid']}/enable/",
        )
        self.assertEqual(action_response.status_code, status.HTTP_403_FORBIDDEN)


class RoleUpdateDescriptionsTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = UserFactory(is_staff=True)
        self.role = models.Role.objects.create(
            name="test_role",
            description_en="Old description in English",
            description_et="Old description in Estonian",
            content_type=ContentType.objects.get_for_model(Project),
        )
        self.url = factories.RoleFactory.get_url(
            self.role,
            action="update_descriptions",
        )

    def test_staff_can_update_role_descriptions(self):
        self.client.force_authenticate(self.staff)
        new_descriptions = {
            "description_en": "New description in English",
            "description_et": "New description in Estonian",
        }

        response = self.client.put(self.url, new_descriptions)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.role.refresh_from_db()
        self.assertEqual(self.role.description_en, new_descriptions["description_en"])
        self.assertEqual(self.role.description_et, new_descriptions["description_et"])

    def test_partial_update_of_descriptions(self):
        self.client.force_authenticate(self.staff)
        partial_update = {"description_en": "Only English updated"}

        response = self.client.put(self.url, partial_update)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.role.refresh_from_db()
        self.assertEqual(self.role.description_en, "Only English updated")
        self.assertEqual(self.role.description_et, "Old description in Estonian")
