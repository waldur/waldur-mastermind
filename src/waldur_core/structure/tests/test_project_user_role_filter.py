from rest_framework import test

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories, fixtures

PROJECT_ADMIN = RoleEnum.PROJECT_ADMIN


class ProjectUserRoleFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        factories.ProjectFactory(customer=self.fixture.customer)
        self.client.force_authenticate(self.fixture.staff)

    def list_projects(self, user, role=PROJECT_ADMIN):
        return self.client.get(
            factories.ProjectFactory.get_list_url(),
            {"user_uuid_with_active_role": user.uuid.hex, "user_role": role},
        ).data

    def test_filter_by_role_name(self):
        self.assertEqual(
            [row["uuid"] for row in self.list_projects(self.admin)],
            [self.project.uuid.hex],
        )

    def test_filter_by_role_uuid(self):
        role = Role.objects.get(name=PROJECT_ADMIN)
        self.assertEqual(
            [row["uuid"] for row in self.list_projects(self.admin, role.uuid.hex)],
            [self.project.uuid.hex],
        )

    def test_filter_ignores_other_roles(self):
        self.assertEqual(self.list_projects(self.manager), [])

    def test_filter_ignores_revoked_administrator(self):
        self.project.remove_user(self.admin)
        self.assertEqual(self.list_projects(self.admin), [])
