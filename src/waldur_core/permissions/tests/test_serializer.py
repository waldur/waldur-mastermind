from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.permissions import models
from waldur_core.permissions.serializers import PermissionSerializer
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


def _make_user_role(user, role, scope):
    return models.UserRole.objects.create(
        user=user,
        role=role,
        content_type=ContentType.objects.get_for_model(scope),
        object_id=scope.id,
    )


class PermissionSerializerProjectUuidTest(TestCase):
    """get_project_uuid resolves the owning Waldur project for resource-flavored
    scopes so the frontend can check project-level roles when deciding whether
    to enable delete/update actions on resource permission rows."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.project = structure_factories.ProjectFactory()
        self.resource = marketplace_factories.ResourceFactory(project=self.project)
        self.resource_project = marketplace_models.ResourceProject.objects.create(
            resource=self.resource, name="Sub-project"
        )

        # A role per scope type. Names are arbitrary — we only serialize.
        self.resource_role = models.Role.objects.create(
            name="Resource Role",
            content_type=ContentType.objects.get_for_model(marketplace_models.Resource),
            is_system_role=False,
        )
        self.resource_project_role = models.Role.objects.create(
            name="ResourceProject Role",
            content_type=ContentType.objects.get_for_model(
                marketplace_models.ResourceProject
            ),
            is_system_role=False,
        )

    def test_returns_owning_project_uuid_for_resource_scope(self):
        user_role = _make_user_role(self.user, self.resource_role, self.resource)
        data = PermissionSerializer(user_role).data
        self.assertEqual(data["project_uuid"], self.project.uuid.hex)

    def test_returns_parent_resource_project_uuid_for_resource_project_scope(self):
        user_role = _make_user_role(
            self.user, self.resource_project_role, self.resource_project
        )
        data = PermissionSerializer(user_role).data
        self.assertEqual(data["project_uuid"], self.project.uuid.hex)

    def test_returns_none_for_customer_scope(self):
        customer_role = models.Role.objects.create(
            name="Customer Role",
            content_type=ContentType.objects.get_for_model(self.project.customer),
            is_system_role=False,
        )
        user_role = _make_user_role(self.user, customer_role, self.project.customer)
        data = PermissionSerializer(user_role).data
        self.assertIsNone(data["project_uuid"])

    def test_returns_none_for_project_scope(self):
        """Project-scoped rows don't need this field — scope_uuid already
        carries the project uuid. Documenting the contract for the frontend."""
        project_role = models.Role.objects.create(
            name="Project Role",
            content_type=ContentType.objects.get_for_model(self.project),
            is_system_role=False,
        )
        user_role = _make_user_role(self.user, project_role, self.project)
        data = PermissionSerializer(user_role).data
        self.assertIsNone(data["project_uuid"])
