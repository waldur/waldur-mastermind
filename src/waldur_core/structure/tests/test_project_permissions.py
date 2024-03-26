import datetime

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure.tests import factories, fixtures
from waldur_core.structure.tests.utils import (
    client_add_user,
    client_delete_user,
    client_list_users,
    client_update_user,
)

User = get_user_model()


class ProjectPermissionBaseTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        super().setUp()

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_PROJECT_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT_PERMISSION)

        ProjectRole.MANAGER.add_permission(PermissionEnum.DELETE_PROJECT_PERMISSION)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_PROJECT_PERMISSION)


class ProjectPermissionListTest(ProjectPermissionBaseTest):
    def test_user_cannot_list_roles_of_project_he_is_not_affiliated(self):
        response = client_list_users(
            self.client, factories.UserFactory(), factories.ProjectFactory()
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_customer_owner_can_list_roles_of_his_customers_project(self):
        owner = factories.UserFactory()
        customer = factories.CustomerFactory()
        customer.add_user(owner, CustomerRole.OWNER)
        project = factories.ProjectFactory(customer=customer)
        response = client_list_users(self.client, owner, project)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_owner_cannot_list_roles_of_another_customers_project(self):
        owner = factories.UserFactory()
        customer = factories.CustomerFactory()
        customer.add_user(owner, CustomerRole.OWNER)
        project = factories.ProjectFactory()
        response = client_list_users(self.client, owner, project)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_admin_can_list_roles_of_his_project(self):
        admin = factories.UserFactory()
        customer = factories.CustomerFactory()
        project = factories.ProjectFactory(customer=customer)
        project.add_user(admin, ProjectRole.ADMIN)
        response = client_list_users(self.client, admin, project)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_project_admin_cannot_list_roles_of_another_project(self):
        admin = factories.UserFactory()
        customer = factories.CustomerFactory()
        project = factories.ProjectFactory(customer=customer)
        project.add_user(admin, ProjectRole.ADMIN)
        response = client_list_users(self.client, admin, factories.ProjectFactory())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_can_list_roles_of_any_project(self):
        customer = factories.CustomerFactory()
        project = factories.ProjectFactory(customer=customer)
        response = client_list_users(
            self.client, factories.UserFactory(is_staff=True), project
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ProjectPermissionGrantTest(ProjectPermissionBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.owner = factories.UserFactory()
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)
        self.customer.add_user(self.owner, CustomerRole.OWNER)

    def test_customer_owner_can_grant_new_role_within_his_customers_project(self):
        response = client_add_user(
            self.client,
            self.owner,
            factories.UserFactory(),
            self.project,
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_customer_owner_can_grant_new_role_within_his_customers_project_for_himself(
        self,
    ):
        response = client_add_user(
            self.client, self.owner, self.owner, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_customer_owner_cannot_grant_existing_role_within_his_project(self):
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)
        response = client_add_user(
            self.client, self.owner, admin, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_owner_can_grant_role_within_his_project_even_if_user_already_has_role(
        self,
    ):
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)
        response = client_add_user(
            self.client, self.owner, admin, self.project, ProjectRole.MANAGER
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_customer_owner_cannot_grant_role_within_another_customers_project(self):
        customer = factories.CustomerFactory()
        project = factories.ProjectFactory(customer=customer)
        response = client_add_user(
            self.client,
            self.owner,
            factories.UserFactory(),
            project,
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_manager_cannot_grant_new_admin_role_within_his_project(self):
        manager = factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)
        response = client_add_user(
            self.client,
            manager,
            factories.UserFactory(),
            self.project,
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_manager_cannot_grant_new_manager_role_within_his_project(self):
        manager = factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)
        response = client_add_user(
            self.client,
            manager,
            factories.UserFactory(),
            self.project,
            ProjectRole.MANAGER,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_manager_cannot_grant_new_admin_role_within_not_his_project(self):
        manager = factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)
        response = client_add_user(
            self.client,
            manager,
            factories.UserFactory(),
            factories.ProjectFactory(customer=self.customer),
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_admin_cannot_grant_new_role_within_his_project(self):
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)
        response = client_add_user(
            self.client,
            admin,
            factories.UserFactory(),
            self.project,
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_manager_cannot_grant_existing_role_within_his_project(self):
        manager = factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)

        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)

        response = client_add_user(
            self.client, manager, admin, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_manager_cannot_grant_role_to_himself(self):
        manager = factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)

        response = client_add_user(
            self.client, manager, manager, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_admin_cannot_grant_role_within_another_project(self):
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)
        response = client_add_user(
            self.client,
            admin,
            factories.UserFactory(),
            factories.ProjectFactory(customer=self.customer),
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_can_grant_new_role_within_any_project(self):
        response = client_add_user(
            self.client,
            factories.UserFactory(is_staff=True),
            factories.UserFactory(),
            self.project,
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_cannot_grant_existing_role_within_any_project(self):
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)

        response = client_add_user(
            self.client,
            factories.UserFactory(is_staff=True),
            admin,
            self.project,
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ProjectPermissionRevokeTest(ProjectPermissionBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)

        self.owner = factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)

    def test_customer_owner_can_revoke_role_within_his_customers_project(self):
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)
        response = client_delete_user(
            self.client, self.owner, admin, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_owner_can_revoke_role_within_his_customers_project_for_himself(
        self,
    ):
        self.project.add_user(self.owner, ProjectRole.ADMIN)
        response = client_delete_user(
            self.client, self.owner, self.owner, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_owner_cannot_revoke_role_within_another_customers_project(self):
        customer = factories.CustomerFactory()
        project = factories.ProjectFactory(customer=customer)
        admin = factories.UserFactory()
        project.add_user(admin, ProjectRole.ADMIN)

        response = client_delete_user(
            self.client, self.owner, admin, project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_manager_can_revoke_admin_role_within_his_project(self):
        manager = factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)

        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)

        response = client_delete_user(
            self.client, manager, admin, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_project_manager_cannot_revoke_manager_role_within_his_project(self):
        manager = factories.UserFactory()
        self.project.add_user(manager, ProjectRole.MANAGER)

        response = client_delete_user(
            self.client, manager, manager, self.project, ProjectRole.MANAGER
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_admin_cannot_revoke_role_within_his_project(self):
        admin1 = factories.UserFactory()
        self.project.add_user(admin1, ProjectRole.ADMIN)

        admin2 = factories.UserFactory()
        self.project.add_user(admin2, ProjectRole.ADMIN)

        response = client_delete_user(
            self.client, admin1, admin2, self.project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_admin_cannot_revoke_role_within_another_project(self):
        admin1 = factories.UserFactory()
        self.project.add_user(admin1, ProjectRole.ADMIN)

        project2 = factories.ProjectFactory(customer=self.customer)
        admin2 = factories.UserFactory()
        project2.add_user(admin2, ProjectRole.ADMIN)

        response = client_delete_user(
            self.client, admin1, admin2, project2, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_can_revoke_role_within_any_project(self):
        admin = factories.UserFactory()
        self.project.add_user(admin, ProjectRole.ADMIN)

        response = client_delete_user(
            self.client,
            factories.UserFactory(is_staff=True),
            admin,
            self.project,
            ProjectRole.ADMIN,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ProjectPermissionExpirationTest(ProjectPermissionBaseTest):
    def setUp(self):
        super().setUp()
        self.customer = factories.CustomerFactory()
        self.project = factories.ProjectFactory(customer=self.customer)

        self.admin = factories.UserFactory()
        self.project.add_user(self.admin, ProjectRole.ADMIN)

    def update_expiration_time(self, current_user, expiration_time):
        return client_update_user(
            self.client,
            current_user,
            self.admin,
            self.project,
            ProjectRole.ADMIN,
            expiration_time,
        )

    def test_user_can_not_update_permission_expiration_time(self):
        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.update_expiration_time(self.admin, expiration_time)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_update_permission_expiration_time(self):
        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = self.update_expiration_time(
            factories.UserFactory(is_staff=True), expiration_time
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["expiration_time"], expiration_time, response.data
        )

    def test_owner_can_update_permission_for_admin(self):
        expiration_time = timezone.now() + datetime.timedelta(days=100)
        owner = factories.UserFactory()
        self.customer.add_user(owner, CustomerRole.OWNER)

        response = self.update_expiration_time(owner, expiration_time)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["expiration_time"], expiration_time, response.data
        )

    def test_user_can_not_set_permission_expiration_time_lower_than_current(self):
        expiration_time = timezone.now() - datetime.timedelta(days=100)
        response = self.update_expiration_time(
            factories.UserFactory(is_staff=True), expiration_time
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_set_expiration_time_role_when_role_is_created(self):
        expiration_time = timezone.now() + datetime.timedelta(days=100)
        response = client_add_user(
            self.client,
            factories.UserFactory(is_staff=True),
            factories.UserFactory(),
            factories.ProjectFactory(),
            ProjectRole.ADMIN,
            expiration_time,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data["expiration_time"], expiration_time, response.data
        )

    def test_user_cannot_grant_permissions_with_greater_expiration_time(self):
        user = factories.UserFactory()
        project = factories.ProjectFactory()
        expiration_time = timezone.now() + datetime.timedelta(days=100)
        project.add_user(user, ProjectRole.MANAGER, expiration_time=expiration_time)
        response = client_add_user(
            self.client,
            user,
            factories.UserFactory(),
            project,
            ProjectRole.ADMIN,
            expiration_time + datetime.timedelta(days=1),
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProjectPermissionCreatedByTest(ProjectPermissionBaseTest):
    def test_user_which_granted_permission_is_stored(self):
        staff_user = factories.UserFactory(is_staff=True)
        user = factories.UserFactory()
        project = factories.ProjectFactory()

        response = client_add_user(
            self.client, staff_user, user, project, ProjectRole.ADMIN
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        permission = UserRole.objects.get(user=user, role=ProjectRole.ADMIN)
        self.assertEqual(permission.created_by, staff_user)


class GetProjectUsersTest(ProjectPermissionBaseTest):
    def setUp(self):
        fixture = fixtures.ProjectFixture()
        self.project = fixture.project
        self.admin = fixture.admin
        self.manager = fixture.manager

    def test_get_users_by_default_returns_both_managers_and_admins(self):
        users = set(self.project.get_users())
        self.assertSetEqual(users, {self.admin, self.manager})

    def test_get_users_by_returns_admins(self):
        users = list(self.project.get_users(ProjectRole.ADMIN))
        self.assertListEqual(users, [self.admin])

    def test_get_users_by_returns_managers(self):
        users = list(self.project.get_users(ProjectRole.MANAGER))
        self.assertListEqual(users, [self.manager])
