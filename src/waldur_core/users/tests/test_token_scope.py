"""Public invitations are reserved to staff. A staff user's personal access
token without `STAFF.ACCESS` acts as the user without staff (#980), even
where it carries the permission that creating the invitation needs.
"""

from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests.pat_utils import STAFF, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models
from waldur_core.users.tests import factories

CREATE_PERMISSION = PermissionEnum.CREATE_CUSTOMER_PERMISSION.value


@override_config(PAT_ENABLED=True)
class PublicInvitationTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.staff = structure_factories.UserFactory(is_staff=True)

    def create(self):
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(),
            {
                "scope": structure_factories.CustomerFactory.get_url(self.customer),
                "role": ProjectRole.MANAGER.uuid.hex,
                "is_public": True,
                "auto_create_project": True,
                "project_role": ProjectRole.MANAGER.uuid.hex,
                "project_name_template": "Test Project",
            },
        )
        return response, models.GroupInvitation.objects.filter(is_public=True)

    def test_staff_session_creates_public_invitation(self):
        self.client.force_authenticate(self.staff)
        response, invitations = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(invitations.exists())

    def test_staff_token_with_staff_scope_creates_public_invitation(self):
        use_pat(self.client, self.staff, STAFF, CREATE_PERMISSION)
        response, invitations = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(invitations.exists())

    def test_staff_token_without_staff_scope_cannot_create_public_invitation(self):
        # It reads as not staff (#987), so it acts as the owner it also is.
        self.customer.add_user(self.staff, CustomerRole.OWNER)
        use_pat(self.client, self.staff, CREATE_PERMISSION)
        response, invitations = self.create()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_public", response.data)
        self.assertFalse(invitations.exists())

    def test_owner_cannot_create_public_invitation(self):
        self.client.force_authenticate(self.owner)
        response, invitations = self.create()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(invitations.exists())
