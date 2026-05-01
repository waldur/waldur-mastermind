from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class ResourceListUsersTest(test.APITestCase):
    """Test list_users on ConsumerResourceViewSet — aggregates UserRoles
    from the resource and all its resource projects."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

        self.resource_ct = ContentType.objects.get_for_model(models.Resource)
        self.rp_ct = ContentType.objects.get_for_model(models.ResourceProject)

        # Create roles
        self.resource_role = Role.objects.create(
            name="Resource Viewer",
            content_type=self.resource_ct,
            is_system_role=False,
        )
        self.project_role = Role.objects.create(
            name="Project Editor",
            content_type=self.rp_ct,
            is_system_role=False,
        )

        # Create resource projects
        self.rp_a = models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )
        self.rp_b = models.ResourceProject.objects.create(
            resource=self.resource, name="Project B"
        )

        # Users
        self.user_a = structure_factories.UserFactory()
        self.user_b = structure_factories.UserFactory()
        self.user_c = structure_factories.UserFactory()

    def _get_list_users_url(self):
        return marketplace_factories.ResourceFactory.get_url(
            self.resource, action="list_users"
        )

    def test_staff_sees_all_user_roles_across_resource_and_projects(self):
        # user_a has role on resource itself
        UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )
        # user_b has role on project A
        UserRole.objects.create(
            user=self.user_b,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )
        # user_c has role on project B
        UserRole.objects.create(
            user=self.user_c,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_b.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

    def test_resource_only_roles_are_included(self):
        UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["user_uuid"], self.user_a.uuid.hex)

    def test_project_only_roles_are_included(self):
        UserRole.objects.create(
            user=self.user_b,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 1)

    def test_roles_from_other_resources_not_included(self):
        other_resource = marketplace_factories.ResourceFactory(
            offering=self.fixture.offering
        )
        other_rp = models.ResourceProject.objects.create(
            resource=other_resource, name="Other"
        )
        # Role on a different resource's project
        UserRole.objects.create(
            user=self.user_a,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=other_rp.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 0)

    def test_inactive_user_roles_not_included(self):
        role = UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )
        role.revoke()

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 0)

    def test_empty_resource_returns_empty_list(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_user_with_roles_in_both_resource_and_project_appears_twice(self):
        UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )
        UserRole.objects.create(
            user=self.user_a,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 2)

    def test_filter_by_user(self):
        UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )
        UserRole.objects.create(
            user=self.user_b,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(
            self._get_list_users_url(), {"user": self.user_a.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["user_uuid"], self.user_a.uuid.hex)
