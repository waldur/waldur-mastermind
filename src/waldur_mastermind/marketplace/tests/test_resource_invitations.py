from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import Role, RoleAvailability, UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models as users_models
from waldur_core.users.enums import InvitationState
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

INVITATION_LIST_URL = "/api/user-invitations/"


def _detail_url(invitation, action=None):
    base = f"{INVITATION_LIST_URL}{invitation.uuid.hex}/"
    return f"{base}{action}/" if action else base


class BaseResourceInvitationTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.offering = self.fixture.offering

        # Roles required for invitation create/list flows on resource scopes.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_RESOURCE_PERMISSION)
        CustomerRole.OWNER.add_permission(
            PermissionEnum.CREATE_RESOURCE_PROJECT_PERMISSION
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_INVITATIONS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_RESOURCE_PERMISSION)
        ProjectRole.MANAGER.add_permission(
            PermissionEnum.CREATE_RESOURCE_PROJECT_PERMISSION
        )
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_INVITATIONS)

        self.resource_ct = ContentType.objects.get_for_model(models.Resource)
        self.rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        self.offering_ct = ContentType.objects.get_for_model(models.Offering)

        self.resource_role = Role.objects.create(
            name="Resource Cluster Admin",
            content_type=self.resource_ct,
            is_system_role=False,
        )
        self.resource_project_role = Role.objects.create(
            name="Project Member",
            content_type=self.rp_ct,
            is_system_role=False,
        )

        self.resource_project = models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )

        self.invitee = structure_factories.UserFactory(email="invitee@example.com")


def _resource_url(resource):
    return marketplace_factories.ResourceFactory.get_url(resource)


def _resource_project_url(rp):
    return "http://testserver" + reverse(
        "marketplace-resource-project-detail", kwargs={"uuid": rp.uuid.hex}
    )


class ResourceInvitationCreateTest(BaseResourceInvitationTest):
    def _payload(self, scope_url, role):
        return {
            "email": "invitee@example.com",
            "scope": scope_url,
            "role": role.uuid.hex,
        }

    def test_customer_owner_can_invite_to_resource(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            INVITATION_LIST_URL,
            self._payload(_resource_url(self.resource), self.resource_role),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        invitation = users_models.Invitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.scope, self.resource)
        self.assertEqual(invitation.role, self.resource_role)
        self.assertEqual(invitation.customer, self.resource.project.customer)

    def test_customer_owner_can_invite_to_resource_project(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            INVITATION_LIST_URL,
            self._payload(
                _resource_project_url(self.resource_project),
                self.resource_project_role,
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        invitation = users_models.Invitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.scope, self.resource_project)
        self.assertEqual(invitation.customer, self.resource.project.customer)

    def test_project_manager_can_invite_to_resource(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(
            INVITATION_LIST_URL,
            self._payload(_resource_url(self.resource), self.resource_role),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_unauthorized_user_cannot_invite(self):
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.post(
            INVITATION_LIST_URL,
            self._payload(_resource_url(self.resource), self.resource_role),
        )
        # InvitationViewSet returns 404 to hide existence; serializer may also
        # return 400 if the resource URL is not visible to the user.
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND),
        )

    def test_role_content_type_must_match_scope(self):
        """Resource-scoped role cannot be used for ResourceProject scope and vice versa."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            INVITATION_LIST_URL,
            self._payload(
                _resource_project_url(self.resource_project), self.resource_role
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("content type", str(response.data).lower())


class ResourceInvitationAcceptTest(BaseResourceInvitationTest):
    def _create_invitation(self, scope, role):
        return users_models.Invitation.objects.create(
            email=self.invitee.email,
            scope=scope,
            customer=self.resource.project.customer,
            role=role,
            created_by=self.fixture.owner,
        )

    def test_accept_creates_user_role_on_resource(self):
        invitation = self._create_invitation(self.resource, self.resource_role)
        self.client.force_authenticate(self.invitee)
        response = self.client.post(_detail_url(invitation, "accept"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.ACCEPTED)
        self.assertTrue(
            UserRole.objects.filter(
                user=self.invitee,
                role=self.resource_role,
                content_type=self.resource_ct,
                object_id=self.resource.id,
                is_active=True,
            ).exists()
        )

    def test_accept_creates_user_role_on_resource_project(self):
        invitation = self._create_invitation(
            self.resource_project, self.resource_project_role
        )
        self.client.force_authenticate(self.invitee)
        response = self.client.post(_detail_url(invitation, "accept"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.assertTrue(
            UserRole.objects.filter(
                user=self.invitee,
                role=self.resource_project_role,
                content_type=self.rp_ct,
                object_id=self.resource_project.id,
                is_active=True,
            ).exists()
        )

    def test_accept_with_matching_offering_role_availability(self):
        RoleAvailability.objects.create(
            role=self.resource_role,
            content_type=self.offering_ct,
            object_id=self.offering.id,
        )
        invitation = self._create_invitation(self.resource, self.resource_role)
        self.client.force_authenticate(self.invitee)
        response = self.client.post(_detail_url(invitation, "accept"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_accept_rejects_mismatched_role_availability(self):
        """Role limited to one offering must not be granted via an
        invitation pointing at another offering's resource."""
        other_offering = marketplace_factories.OfferingFactory()
        RoleAvailability.objects.create(
            role=self.resource_role,
            content_type=self.offering_ct,
            object_id=other_offering.id,
        )
        invitation = self._create_invitation(self.resource, self.resource_role)
        self.client.force_authenticate(self.invitee)
        response = self.client.post(_detail_url(invitation, "accept"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not available", str(response.data).lower())
        self.assertFalse(
            UserRole.objects.filter(
                user=self.invitee,
                role=self.resource_role,
                content_type=self.resource_ct,
                object_id=self.resource.id,
                is_active=True,
            ).exists()
        )
        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.PENDING)

    def test_accept_rejects_inactive_role(self):
        self.resource_role.is_active = False
        self.resource_role.save(update_fields=["is_active"])
        invitation = self._create_invitation(self.resource, self.resource_role)
        self.client.force_authenticate(self.invitee)
        response = self.client.post(_detail_url(invitation, "accept"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not active", str(response.data).lower())


class ResourceInvitationCancelTest(BaseResourceInvitationTest):
    def test_customer_owner_can_cancel_invitation(self):
        invitation = users_models.Invitation.objects.create(
            email="invitee@example.com",
            scope=self.resource,
            customer=self.resource.project.customer,
            role=self.resource_role,
            created_by=self.fixture.owner,
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(_detail_url(invitation, "cancel"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.CANCELED)

    def test_unauthorized_user_cannot_cancel(self):
        invitation = users_models.Invitation.objects.create(
            email="invitee@example.com",
            scope=self.resource,
            customer=self.resource.project.customer,
            role=self.resource_role,
            created_by=self.fixture.owner,
        )
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.post(_detail_url(invitation, "cancel"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ResourceInvitationListTest(BaseResourceInvitationTest):
    def test_customer_owner_sees_invitation(self):
        users_models.Invitation.objects.create(
            email="invitee@example.com",
            scope=self.resource,
            customer=self.resource.project.customer,
            role=self.resource_role,
            created_by=self.fixture.owner,
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(INVITATION_LIST_URL, {"scope_type": "resource"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emails = [inv["email"] for inv in response.data]
        self.assertIn("invitee@example.com", emails)

    def test_filter_by_scope_type_resource_project(self):
        users_models.Invitation.objects.create(
            email="invitee@example.com",
            scope=self.resource_project,
            customer=self.resource.project.customer,
            role=self.resource_project_role,
            created_by=self.fixture.owner,
        )
        users_models.Invitation.objects.create(
            email="other@example.com",
            scope=self.resource,
            customer=self.resource.project.customer,
            role=self.resource_role,
            created_by=self.fixture.owner,
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            INVITATION_LIST_URL, {"scope_type": "resource_project"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emails = [inv["email"] for inv in response.data]
        self.assertEqual(emails, ["invitee@example.com"])


class ResourceProjectCustomerPropertyTest(test.APITestCase):
    """Sanity check that ResourceProject.customer/project properties work,
    so users.utils.can_manage_invitation_with does not raise AttributeError."""

    def test_resource_project_exposes_customer_and_project(self):
        fixture = marketplace_fixtures.MarketplaceFixture()
        rp = models.ResourceProject.objects.create(resource=fixture.resource, name="P")
        self.assertEqual(rp.customer, fixture.resource.project.customer)
        self.assertEqual(rp.project, fixture.resource.project)
