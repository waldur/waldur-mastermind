from django.core.exceptions import ValidationError
from rest_framework import status, test
from rest_framework.test import APIClient

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models
from waldur_core.users.tests import factories


class PublicGroupInvitationTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.regular_user = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory()
        self.customer_owner = structure_factories.UserFactory()
        self.customer.add_user(self.customer_owner, CustomerRole.OWNER)

        # Add required permissions
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_INVITATIONS)

        # Create public group invitation
        self.public_invitation = models.GroupInvitation.objects.create(
            customer=self.customer,
            scope=self.customer,
            role=ProjectRole.MANAGER,  # Use project-level role for public invitations
            is_public=True,
            auto_create_project=True,
            project_role=ProjectRole.MANAGER,
            project_name_template="{user.full_name}'s Project",
            created_by=self.staff,
        )

        # Create private group invitation
        self.private_invitation = models.GroupInvitation.objects.create(
            customer=self.customer,
            scope=self.customer,
            role=CustomerRole.OWNER,
            is_public=False,
            created_by=self.customer_owner,
        )

    def test_unauthenticated_user_can_list_public_invitations(self):
        """Test that unauthenticated users can see public invitations in list view."""
        client = APIClient()

        response = client.get(factories.GroupInvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only see public invitations
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.public_invitation.uuid.hex)
        self.assertTrue(response.data[0]["is_public"])

    def test_unauthenticated_user_can_retrieve_public_invitation(self):
        """Test that unauthenticated users can retrieve specific public invitation."""
        client = APIClient()

        response = client.get(
            factories.CustomerGroupInvitationFactory.get_url(self.public_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["uuid"], self.public_invitation.uuid.hex)
        self.assertTrue(response.data["is_public"])

    def test_unauthenticated_user_cannot_retrieve_private_invitation(self):
        """Test that unauthenticated users cannot retrieve private invitations."""
        client = APIClient()

        response = client.get(
            factories.CustomerGroupInvitationFactory.get_url(self.private_invitation)
        )
        # Should get 404 because the invitation is not public and not accessible
        # The specific filter logic may vary, but unauthenticated users should not access private invitations
        self.assertIn(
            response.status_code,
            [
                status.HTTP_401_UNAUTHORIZED,
                status.HTTP_403_FORBIDDEN,
                status.HTTP_404_NOT_FOUND,
            ],
        )

    def test_authenticated_user_can_see_both_public_and_private_invitations(self):
        """Test that authenticated users can see both public and private invitations they have access to."""
        self.client.force_authenticate(user=self.customer_owner)

        response = self.client.get(factories.GroupInvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should see both invitations
        self.assertEqual(len(response.data), 2)
        uuids = {inv["uuid"] for inv in response.data}
        self.assertIn(self.public_invitation.uuid.hex, uuids)
        self.assertIn(self.private_invitation.uuid.hex, uuids)

    def test_only_staff_can_create_public_invitations(self):
        """Test that only staff users can create public invitations."""
        # Staff can create public invitations
        self.client.force_authenticate(user=self.staff)

        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": ProjectRole.MANAGER.uuid.hex,  # Use project-level role
            "is_public": True,
            "auto_create_project": True,
            "project_role": ProjectRole.MANAGER.uuid.hex,
            "project_name_template": "Test Project",
        }

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["is_public"])

    def test_non_staff_cannot_create_public_invitations(self):
        """Test that non-staff users cannot create public invitations."""
        self.client.force_authenticate(user=self.customer_owner)

        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": ProjectRole.MANAGER.uuid.hex,  # Use project-level role
            "is_public": True,
            "auto_create_project": True,
            "project_role": ProjectRole.MANAGER.uuid.hex,
            "project_name_template": "Test Project",
        }

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_public", response.data)
        self.assertIn(
            "Only staff users can create public invitations", str(response.data)
        )

    def test_public_invitation_must_have_auto_create_project(self):
        """Test that public invitations must have auto_create_project enabled."""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,  # Use customer-level role to avoid role/scope mismatch
            "is_public": True,
            "auto_create_project": False,  # This should cause validation error
        }

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # This should trigger the auto_create_project validation first
        self.assertIn("auto_create_project", response.data)
        self.assertIn(
            "Public invitations must have auto_create_project enabled",
            str(response.data),
        )

    def test_public_invitation_with_project_role_must_have_auto_create_project(self):
        """Test that public invitations with project role must have auto_create_project enabled."""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": ProjectRole.MANAGER.uuid.hex,  # Project-level role
            "is_public": True,
            "auto_create_project": False,  # This should cause validation error
        }

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # This should trigger the auto_create_project validation
        self.assertIn("auto_create_project", response.data)
        self.assertIn(
            "Public invitations must have auto_create_project enabled",
            str(response.data),
        )

    def test_model_validation_for_public_invitations(self):
        """Test model-level validation for public invitations."""
        # Should raise validation error when is_public=True and auto_create_project=False
        invitation = models.GroupInvitation(
            customer=self.customer,
            scope=self.customer,
            role=ProjectRole.MANAGER,  # Use project-level role
            is_public=True,
            auto_create_project=False,
        )

        with self.assertRaises(ValidationError) as context:
            invitation.clean()

        self.assertIn("auto_create_project", context.exception.message_dict)

    def test_public_invitation_cannot_use_customer_level_roles(self):
        """Test that public invitations cannot use customer-level roles."""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,  # Customer-level role should be rejected
            "is_public": True,
            "auto_create_project": True,
            "project_role": ProjectRole.MANAGER.uuid.hex,
            "project_name_template": "Test Project",
        }

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("role", response.data)
        self.assertIn(
            "Public invitations can only use project-level roles", str(response.data)
        )

    def test_model_validation_customer_level_roles_for_public_invitations(self):
        """Test model-level validation prevents customer-level roles for public invitations."""
        # Should raise validation error when is_public=True with customer-level role
        invitation = models.GroupInvitation(
            customer=self.customer,
            scope=self.customer,
            role=CustomerRole.OWNER,  # Customer-level role should be rejected
            is_public=True,
            auto_create_project=True,
            project_role=ProjectRole.MANAGER,
        )

        with self.assertRaises(ValidationError) as context:
            invitation.clean()

        self.assertIn("role", context.exception.message_dict)
        self.assertIn(
            "Public invitations can only use project-level roles",
            str(context.exception.message_dict["role"]),
        )

    def test_authenticated_user_can_submit_request_for_public_invitation(self):
        """Test that authenticated users can submit requests for public invitations."""
        self.client.force_authenticate(user=self.regular_user)

        response = self.client.post(
            f"{factories.CustomerGroupInvitationFactory.get_url(self.public_invitation)}submit_request/"
        )

        # This might fail due to user pattern matching, but should not fail due to authentication
        # The specific response depends on user pattern configuration
        self.assertIn(
            response.status_code, [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST]
        )

        if response.status_code == status.HTTP_400_BAD_REQUEST:
            # Should be due to pattern matching, not authentication
            self.assertNotIn("Authentication required", str(response.data))
            self.assertNotIn("credentials", str(response.data).lower())

    def test_unauthenticated_user_cannot_submit_request(self):
        """Test that unauthenticated users cannot submit requests even for public invitations."""
        client = APIClient()

        response = client.post(
            f"{factories.CustomerGroupInvitationFactory.get_url(self.public_invitation)}submit_request/"
        )

        # Should require authentication
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_user_cannot_perform_management_actions(self):
        """Test that unauthenticated users cannot perform management actions on public invitations."""
        client = APIClient()

        # Cannot cancel
        response = client.post(
            f"{factories.CustomerGroupInvitationFactory.get_url(self.public_invitation)}cancel/"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # Cannot create
        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": ProjectRole.MANAGER.uuid.hex,  # Use project-level role
            "is_public": True,
            "auto_create_project": True,
        }

        response = client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_scope_image_field_is_exposed_in_serialization(self):
        """Test that scope_image field is included in serialization when scope has an image."""
        self.client.force_authenticate(user=self.customer_owner)

        response = self.client.get(
            factories.CustomerGroupInvitationFactory.get_url(self.public_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # scope_image field should be present (None if no image is set)
        self.assertIn("scope_image", response.data)
        # Should be None if no image is attached to the customer
        self.assertIsNone(response.data["scope_image"])

    def test_scope_image_field_present_in_list_view(self):
        """Test that scope_image field is present in list view."""
        client = APIClient()

        response = client.get(factories.GroupInvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        # scope_image field should be present
        self.assertIn("scope_image", response.data[0])
        # Should be None if no image is attached to the customer
        self.assertIsNone(response.data[0]["scope_image"])
