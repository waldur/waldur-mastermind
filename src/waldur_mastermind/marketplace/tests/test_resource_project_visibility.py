"""End-to-end visibility for users whose only role is on a Resource or
ResourceProject.

A user invited as e.g. "Project Member" of a ResourceProject must be able
to render the Homeport UI. That requires basic-detail visibility of the
parent Resource, Project and Customer through the standard list/detail
endpoints.
"""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


def _customer_url(c):
    return "http://testserver" + reverse("customer-detail", kwargs={"uuid": c.uuid.hex})


def _project_url(p):
    return "http://testserver" + reverse("project-detail", kwargs={"uuid": p.uuid.hex})


def _resource_url(r):
    return marketplace_factories.ResourceFactory.get_url(r)


def _resource_project_url(rp):
    return "http://testserver" + reverse(
        "marketplace-resource-project-detail", kwargs={"uuid": rp.uuid.hex}
    )


class ResourceProjectVisibilityTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.project = self.resource.project
        self.customer = self.project.customer
        self.resource_project = models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )
        self.unrelated_customer = structure_factories.CustomerFactory()
        self.unrelated_project = structure_factories.ProjectFactory(
            customer=self.unrelated_customer
        )
        self.unrelated_resource = marketplace_factories.ResourceFactory(
            project=self.unrelated_project
        )

        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        resource_ct = ContentType.objects.get_for_model(models.Resource)
        self.rp_role = Role.objects.create(
            name="Project Member", content_type=rp_ct, is_system_role=False
        )
        self.resource_role = Role.objects.create(
            name="Cluster Admin", content_type=resource_ct, is_system_role=False
        )

        # Invitee with ONLY a ResourceProject role.
        self.rp_invitee = structure_factories.UserFactory()
        add_user(self.resource_project, self.rp_invitee, self.rp_role)

        # Invitee with ONLY a direct Resource role.
        self.r_invitee = structure_factories.UserFactory()
        add_user(self.resource, self.r_invitee, self.resource_role)


class ResourceProjectInviteeVisibilityTest(ResourceProjectVisibilityTest):
    """User with only a UserRole on a ResourceProject."""

    def test_can_get_resource_project(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_resource_project_url(self.resource_project))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.resource_project.uuid.hex)

    def test_can_get_parent_resource(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_resource_url(self.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_can_get_parent_project(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_project_url(self.project))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.project.uuid.hex)

    def test_can_get_parent_customer(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_customer_url(self.customer))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.customer.uuid.hex)

    def test_cannot_see_unrelated_customer(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_customer_url(self.unrelated_customer))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_see_unrelated_project(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_project_url(self.unrelated_project))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_see_unrelated_resource(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_resource_url(self.unrelated_resource))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class DirectResourceInviteeVisibilityTest(ResourceProjectVisibilityTest):
    """User with only a UserRole on a Resource (no ResourceProject)."""

    def test_can_get_resource(self):
        self.client.force_authenticate(self.r_invitee)
        response = self.client.get(_resource_url(self.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_can_get_parent_project(self):
        self.client.force_authenticate(self.r_invitee)
        response = self.client.get(_project_url(self.project))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_can_get_parent_customer(self):
        self.client.force_authenticate(self.r_invitee)
        response = self.client.get(_customer_url(self.customer))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_does_not_see_unrelated_resources_in_project(self):
        sibling = marketplace_factories.ResourceFactory(project=self.project)
        self.client.force_authenticate(self.r_invitee)
        response = self.client.get(_resource_url(sibling))
        # Sibling resources in the same project are NOT visible — Resource role
        # confers visibility to that resource only, and the ancestor chain.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ResourceProjectListVisibilityTest(ResourceProjectVisibilityTest):
    """Listing endpoints should include the role-holder's reachable scopes."""

    def test_customer_list_includes_parent_customer(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.customer.uuid.hex, uuids)
        self.assertNotIn(self.unrelated_customer.uuid.hex, uuids)

    def test_project_list_includes_parent_project(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get("/api/projects/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.project.uuid.hex, uuids)
        self.assertNotIn(self.unrelated_project.uuid.hex, uuids)

    def test_marketplace_resources_list_includes_parent_resource(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get("/api/marketplace-resources/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.resource.uuid.hex, uuids)
        self.assertNotIn(self.unrelated_resource.uuid.hex, uuids)

    def test_resource_projects_list_includes_own_project(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get("/api/marketplace-resource-projects/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.resource_project.uuid.hex, uuids)


class ResourceProjectInviteeRedactionTest(ResourceProjectVisibilityTest):
    """Sensitive resource fields must be redacted for RP-only viewers."""

    REDACTED = (
        "limits",
        "current_usages",
        "limit_usage",
        "backend_metadata",
        "report",
        "available_actions",
        "endpoints",
        "options",
        "error_message",
        "error_traceback",
        "username",
    )

    def _populate_sensitive_fields(self):
        self.resource.limits = {"cpu": 4, "ram": 8192}
        self.resource.current_usages = {"cpu": 2}
        self.resource.backend_metadata = {"backend_id": "abc-123"}
        self.resource.report = [{"header": "h", "body": "secret"}]
        self.resource.error_message = "internal error trace"
        self.resource.error_traceback = "Traceback…"
        self.resource.options = {"raw": "value"}
        self.resource.save()

    def test_rp_only_viewer_does_not_see_sensitive_fields(self):
        self._populate_sensitive_fields()
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_resource_url(self.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for field in self.REDACTED:
            self.assertNotIn(field, response.data, f"{field} leaked to RP viewer")

    def test_direct_resource_invitee_still_sees_sensitive_fields(self):
        self._populate_sensitive_fields()
        self.client.force_authenticate(self.r_invitee)
        response = self.client.get(_resource_url(self.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Direct resource role-holders are not "RP-only" — keep operational
        # payload, but error_traceback is staff-only regardless.
        self.assertIn("limits", response.data)
        self.assertIn("backend_metadata", response.data)
        self.assertNotIn("error_traceback", response.data)

    def test_project_member_does_not_see_error_traceback(self):
        self._populate_sensitive_fields()
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.get(_resource_url(self.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("limits", response.data)
        self.assertIn("backend_metadata", response.data)
        # error_traceback is staff/support only — even project members are
        # not allowed to see internal stack traces.
        self.assertNotIn("error_traceback", response.data)

    def test_staff_sees_error_traceback(self):
        self._populate_sensitive_fields()
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(_resource_url(self.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("error_traceback", response.data)
        self.assertEqual(response.data["error_traceback"], "Traceback…")


class OutsiderVisibilityTest(ResourceProjectVisibilityTest):
    """User with no role anywhere sees nothing."""

    def setUp(self):
        super().setUp()
        self.outsider = structure_factories.UserFactory()

    def test_customer_list_empty(self):
        self.client.force_authenticate(self.outsider)
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_resource_projects_list_empty(self):
        self.client.force_authenticate(self.outsider)
        response = self.client.get("/api/marketplace-resource-projects/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])


class ResourceProjectInviteeListUsersGateTest(ResourceProjectVisibilityTest):
    """Security regression: an RP-only invitee must NOT enumerate the parent
    customer / project / resource team via the list_users action.

    The visibility patches grant read access to the parent project/customer
    so the homeport UI can render breadcrumbs, but list_users is now
    explicitly gated by direct membership on the scope or an enclosing
    project/customer (see UserRoleMixin in waldur_core.permissions.views).
    """

    def test_rp_invitee_cannot_list_customer_users(self):
        self.client.force_authenticate(self.rp_invitee)
        url = _customer_url(self.customer) + "list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_rp_invitee_cannot_list_project_users(self):
        self.client.force_authenticate(self.rp_invitee)
        url = _project_url(self.project) + "list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_rp_invitee_cannot_list_resource_users(self):
        self.client.force_authenticate(self.rp_invitee)
        url = _resource_url(self.resource) + "list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_direct_resource_invitee_cannot_list_customer_users(self):
        self.client.force_authenticate(self.r_invitee)
        url = _customer_url(self.customer) + "list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_project_member_can_list_project_users(self):
        # Existing project member retains list_users access.
        self.client.force_authenticate(self.fixture.admin)
        url = _project_url(self.project) + "list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_owner_can_list_customer_users(self):
        self.client.force_authenticate(self.fixture.owner)
        url = _customer_url(self.customer) + "list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_list_users(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        url = _customer_url(self.customer) + "list_users/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ResourceProjectInviteeOrderWorkflowTest(ResourceProjectVisibilityTest):
    """Order workflow fields stay hidden from resource-only role holders."""

    def test_rp_only_viewer_does_not_see_creation_order(self):
        self.client.force_authenticate(self.rp_invitee)
        response = self.client.get(_resource_url(self.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("creation_order", response.data)
        self.assertIsNone(response.data["creation_order"])

    def test_project_member_sees_creation_order(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(_resource_url(self.resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data["creation_order"])
        self.assertEqual(
            response.data["creation_order"]["uuid"], self.fixture.order.uuid.hex
        )
