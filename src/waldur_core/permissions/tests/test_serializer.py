from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from waldur_core.permissions import models
from waldur_core.permissions.serializers import (
    MePermissionSerializer,
    PermissionSerializer,
)
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.tests import factories as proposal_factories


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


class PermissionSerializerProposalCustomerTest(TestCase):
    """PermissionSerializer reads `scope.customer` for every role, so a scope
    without that attribute is listed with no organisation at all. Proposals
    were the case: `Proposal.Permissions.customer_path` already named the
    chain, but nothing exposed it on the instance."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.proposal = proposal_factories.ProposalFactory()
        self.role = models.Role.objects.create(
            name="Proposal Role",
            content_type=ContentType.objects.get_for_model(proposal_models.Proposal),
            is_system_role=False,
        )

    def test_reports_the_call_managing_organisation(self):
        customer = self.proposal.round.call.manager.customer
        user_role = _make_user_role(self.user, self.role, self.proposal)

        data = PermissionSerializer(user_role).data

        self.assertEqual(data["customer_uuid"], customer.uuid.hex)
        self.assertEqual(data["customer_name"], customer.name)

    def test_agrees_with_the_permission_customer_path(self):
        """The serializer and `Permissions.customer_path` must name the same
        organisation, or role listing and role filtering disagree about which
        organisation a proposal belongs to."""
        path = proposal_models.Proposal.Permissions.customer_path
        user_role = _make_user_role(self.user, self.role, self.proposal)

        serialized = PermissionSerializer(user_role).data["customer_uuid"]
        by_path = proposal_models.Proposal.objects.filter(
            pk=self.proposal.pk, **{f"{path}__uuid": serialized}
        )

        self.assertTrue(by_path.exists())

    def test_leaves_the_model_without_a_customer_attribute(self):
        """Resolving this in the serializer is the whole point: `Proposal` must
        not grow a `customer`, because `get_scope_ancestors` appends
        `scope.customer` when it exists and `pat_filtering` mirrors that walk —
        the two document Proposal as having no ancestor inheritance."""
        self.assertFalse(hasattr(self.proposal, "customer"))


class PermissionSerializerCustomerScopeTest(TestCase):
    """When the role scope *is* the organisation, customer_uuid must be that
    organisation — not null. Emitting null broke SDK clients that parse
    /api/users/me/ permissions (UUID(None) on MePermission.customer_uuid)."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.customer_role = models.Role.objects.create(
            name="Customer Scope Role",
            content_type=ContentType.objects.get_for_model(structure_models.Customer),
            is_system_role=False,
        )
        self.project_role = models.Role.objects.create(
            name="Project Scope Role",
            content_type=ContentType.objects.get_for_model(structure_models.Project),
            is_system_role=False,
        )

    def test_customer_scoped_role_reports_the_organisation(self):
        user_role = _make_user_role(self.user, self.customer_role, self.customer)
        data = PermissionSerializer(user_role).data
        self.assertEqual(data["customer_uuid"], self.customer.uuid.hex)
        self.assertEqual(data["customer_name"], self.customer.name)

    def test_project_scoped_role_still_reports_its_organisation(self):
        user_role = _make_user_role(self.user, self.project_role, self.project)
        data = PermissionSerializer(user_role).data
        self.assertEqual(data["customer_uuid"], self.customer.uuid.hex)
        self.assertEqual(data["customer_name"], self.customer.name)

    def test_me_permission_serializer_includes_customer_uuid_for_customer_scope(self):
        """Same resolver backs /api/users/me/ — the path site-agent E2E hits."""
        user_role = _make_user_role(self.user, self.customer_role, self.customer)
        data = MePermissionSerializer(user_role).data
        self.assertEqual(data["customer_uuid"], self.customer.uuid.hex)
