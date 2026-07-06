from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class ProjectPosixGroupsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.offering = factories.OfferingFactory(name="HPC Cluster")

        # Project-mapped group GID.
        group = models.OfferingUserGroup.objects.create(
            offering=self.offering, backend_metadata={"gid": 8001}
        )
        group.projects.add(self.project)

        # Role group GID scoped to a resource in the project.
        self.resource = factories.ResourceFactory(
            offering=self.offering, project=self.project
        )
        resource_ct = ContentType.objects.get_for_model(models.Resource)
        self.role = Role.objects.create(
            name="ClusterAdmin", content_type=resource_ct, is_system_role=False
        )
        models.OfferingRoleGroup.objects.create(
            offering=self.offering,
            content_type=resource_ct,
            object_id=self.resource.id,
            role=self.role,
            backend_metadata={"gid": 60001},
        )

    def get_url(self):
        return reverse("marketplace-project-posix-group-list")

    def request(self, user):
        actor = getattr(self.fixture, user) if isinstance(user, str) else user
        self.client.force_authenticate(actor)
        return self.client.get(self.get_url(), {"project_uuid": self.project.uuid.hex})

    def test_rollup_lists_project_and_role_group_gids(self):
        response = self.request("staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        by_kind = {row["kind"]: row for row in response.data}
        self.assertEqual(by_kind["project_group"]["gid"], 8001)
        self.assertEqual(by_kind["project_group"]["offering_name"], "HPC Cluster")
        self.assertIsNone(by_kind["project_group"]["role"])
        self.assertEqual(by_kind["role_group"]["gid"], 60001)
        self.assertEqual(by_kind["role_group"]["role"], "ClusterAdmin")
        self.assertEqual(by_kind["role_group"]["scope_type"], "resource")
        self.assertEqual(by_kind["role_group"]["scope_uuid"], self.resource.uuid.hex)

    def test_project_manager_can_view(self):
        response = self.request("manager")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_groups_without_gid_are_skipped(self):
        models.OfferingUserGroup.objects.create(
            offering=self.offering, backend_metadata={}
        ).projects.add(self.project)
        response = self.request("staff")
        self.assertEqual(len(response.data), 2)

    def test_unconnected_user_is_forbidden(self):
        response = self.request(structure_factories.UserFactory())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_uuid_is_required(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
