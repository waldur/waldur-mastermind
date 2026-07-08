from unittest import mock

from constance.test.unittest import override_config
from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.test import override_settings
from rest_framework import status

from waldur_core.core.enums import ReviewStates
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import Role, UserRole
from waldur_core.permissions.utils import add_user, has_user
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models, tasks, utils
from waldur_core.users.tests import factories

from .test_invitation import BaseInvitationTest


class BaseGroupInvitationTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.customer_group_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer
        )

        self.project_group_invitation = factories.ProjectGroupInvitationFactory(
            scope=self.project
        )

        factories.CustomerGroupInvitationFactory()


@ddt
class GroupInvitationRetrieveTest(BaseGroupInvitationTest):
    def test_staff_can_get_all_group_invitations(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(factories.GroupInvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

    def test_owner_can_get_only_his_group_invitations(self):
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.get(factories.GroupInvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_unauthorized_user_can_not_list_group_invitations(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(factories.GroupInvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    @data("staff", "customer_owner", "project_admin", "project_manager", "user")
    def test_authorized_user_can_retrieve_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("staff", "customer_owner", "project_admin", "project_manager", "user")
    def test_authorized_user_can_retrieve_customer_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(
            factories.CustomerGroupInvitationFactory.get_url(
                self.customer_group_invitation
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filtering_by_customer_uuid_includes_project_invitations_for_that_customer_too(
        self,
    ):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_list_url(),
            {"customer_uuid": self.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_filtering_by_another_customer_does_not_includes_project_invitations_for_initial_customer(
        self,
    ):
        other_customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_list_url(),
            {"customer_uuid": other_customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_user_can_not_list_projects_of_project_group_invitation(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(
                self.project_group_invitation, action="projects"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_can_list_projects_of_customers_group_invitation(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(
                self.customer_group_invitation, action="projects"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_unauthenticated_user_does_not_see_creator_on_public_invitation(self):
        public_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            role=ProjectRole.ADMIN,
            auto_create_project=True,
            is_public=True,
        )
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(public_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["created_by_full_name"])
        self.assertIsNone(response.data["created_by_username"])
        self.assertIsNone(response.data["created_by_image"])

    def test_staff_sees_creator_on_public_invitation(self):
        public_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            role=ProjectRole.ADMIN,
            auto_create_project=True,
            is_public=True,
            created_by=self.staff,
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(public_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data["created_by_full_name"])

    def test_owner_sees_creator_on_own_public_invitation(self):
        public_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            role=ProjectRole.ADMIN,
            auto_create_project=True,
            is_public=True,
            created_by=self.customer_owner,
        )
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(public_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data["created_by_full_name"])

    def test_regular_user_does_not_see_creator_on_public_invitation(self):
        public_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            role=ProjectRole.ADMIN,
            auto_create_project=True,
            is_public=True,
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(public_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["created_by_full_name"])
        self.assertIsNone(response.data["created_by_username"])
        self.assertIsNone(response.data["created_by_image"])

    def test_custom_text_in_response(self):
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            custom_text="Welcome to our organization!",
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["custom_text"], "Welcome to our organization!")


@ddt
class GroupInvitationCreateTest(BaseGroupInvitationTest):
    @data("staff", "customer_owner")
    def test_user_with_access_can_create_project_admin_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)

        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation,
            role=ProjectRole.ADMIN,
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "customer_owner")
    def test_user_with_access_can_create_project_manager_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)

        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation,
            role=ProjectRole.MANAGER,
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_owner_can_create_project_manager_invitation(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation,
            role=ProjectRole.MANAGER,
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_config(ONLY_ONE_PROJECT_MANAGER=True)
    def test_cannot_create_project_manager_group_invitation_when_limit_reached(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation,
            role=ProjectRole.MANAGER,
        )

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["non_field_errors"][0],
            "Project already has an active project manager.",
        )

    @override_config(ONLY_ONE_PROJECT_MANAGER=True)
    def test_can_create_project_admin_group_invitation_when_limit_reached(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation,
            role=ProjectRole.ADMIN,
        )

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_project_admin_cannot_create_project_invitation(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.project_admin)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_manager_can_create_project_invitation(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.project_manager)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        # Project managers have CREATE_PROJECT_PERMISSION so they can create invitations
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data(
        "user",
    )
    def test_unauthorized_user_cannot_create_project_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("staff", "customer_owner")
    def test_user_with_access_can_create_customer_owner_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_owner_can_not_create_customer_owner_invitation(
        self,
    ):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("staff", "customer_owner")
    def test_user_which_created_invitation_is_stored_in_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        invitation = models.GroupInvitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.created_by, getattr(self, user))

    @data("project_admin", "project_manager")
    def test_user_without_access_cannot_create_customer_owner_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "user",
    )
    def test_unauthorized_user_cannot_create_customer_owner_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_create_invitation_without_scope(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        payload.pop("scope")

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_create_project_invitation_without_role(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        payload.pop("role")

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_cannot_create_customer_invitation_without_customer_role(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        payload.pop("role")

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_waldur_core_settings(ONLY_STAFF_CAN_INVITE_USERS=True)
    def test_if_only_staff_can_create_invitation_then_owner_creates_invitation_request(
        self,
    ):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        invitation = models.GroupInvitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.is_active, True)

    # Helper methods
    def _get_valid_project_invitation_payload(
        self, invitation: models.Invitation | None = None, role: Role | None = None
    ):
        invitation = invitation or factories.ProjectInvitationFactory.build()
        role = role or ProjectRole.ADMIN
        return {
            "scope": structure_factories.ProjectFactory.get_url(invitation.scope),
            "role": role.uuid.hex,
        }

    def _get_valid_customer_invitation_payload(
        self, invitation: models.Invitation | None = None, role: Role | None = None
    ):
        invitation = invitation or factories.CustomerInvitationFactory.build()
        role = role or CustomerRole.OWNER
        return {
            "scope": structure_factories.CustomerFactory.get_url(invitation.scope),
            "role": role.uuid.hex,
        }


@ddt
class GroupInvitationCancelTest(BaseGroupInvitationTest):
    @data("staff", "customer_owner")
    def test_user_with_access_can_cancel_project_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)

        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation, action="cancel"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_group_invitation.refresh_from_db()
        self.assertEqual(self.project_group_invitation.is_active, False)

    @data("project_admin", "user")
    def test_user_without_access_cannot_cancel_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation, action="cancel"
            )
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_manager_can_cancel_project_invitation(self):
        self.client.force_authenticate(user=self.project_manager)
        response = self.client.post(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation, action="cancel"
            )
        )
        # Project managers have CREATE_PROJECT_PERMISSION so they can cancel invitations
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_group_invitation.refresh_from_db()
        self.assertEqual(self.project_group_invitation.is_active, False)

    @data("staff", "customer_owner")
    def test_user_with_access_can_cancel_customer_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.CustomerGroupInvitationFactory.get_url(
                self.customer_group_invitation, action="cancel"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer_group_invitation.refresh_from_db()
        self.assertEqual(self.customer_group_invitation.is_active, False)

    def test_owner_can_not_cancel_customer_invitation(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(
            factories.CustomerGroupInvitationFactory.get_url(
                self.customer_group_invitation, action="cancel"
            )
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class GroupInvitationDeleteTest(BaseGroupInvitationTest):
    @data("staff", "customer_owner")
    def test_user_with_access_can_delete_inactive_project_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.project_group_invitation.cancel()

        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.delete(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation
            )
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.GroupInvitation.objects.filter(
                uuid=self.project_group_invitation.uuid
            ).exists()
        )

    @data("staff", "customer_owner")
    def test_user_with_access_cannot_delete_active_project_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)

        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.delete(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation
            )
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(
            models.GroupInvitation.objects.filter(
                uuid=self.project_group_invitation.uuid
            ).exists()
        )

    @data("project_admin", "user")
    def test_user_without_access_cannot_delete_project_invitation(self, user):
        self.project_group_invitation.cancel()

        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.delete(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation
            )
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_project_manager_can_delete_inactive_project_invitation(self):
        self.project_group_invitation.cancel()

        self.client.force_authenticate(user=self.project_manager)
        response = self.client.delete(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation
            )
        )
        # Project managers have CREATE_PROJECT_PERMISSION so they can delete invitations
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.GroupInvitation.objects.filter(
                uuid=self.project_group_invitation.uuid
            ).exists()
        )

    @data("staff", "customer_owner")
    def test_user_with_access_can_delete_inactive_customer_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.customer_group_invitation.cancel()

        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.delete(
            factories.CustomerGroupInvitationFactory.get_url(
                self.customer_group_invitation
            )
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.GroupInvitation.objects.filter(
                uuid=self.customer_group_invitation.uuid
            ).exists()
        )

    def test_owner_cannot_delete_customer_invitation_without_permission(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.customer_group_invitation.cancel()

        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.delete(
            factories.CustomerGroupInvitationFactory.get_url(
                self.customer_group_invitation
            )
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_deleting_group_invitation_cascades_to_permission_requests(self):
        """Test that deleting a group invitation also deletes related permission requests."""
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)

        # Create a permission request for the invitation
        permission_request = factories.PermissionRequestFactory(
            invitation=self.project_group_invitation
        )

        # Cancel the invitation (required before deletion)
        self.project_group_invitation.cancel()

        # Delete the invitation
        self.client.force_authenticate(user=self.staff)
        response = self.client.delete(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation
            )
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify both invitation and permission request are deleted
        self.assertFalse(
            models.GroupInvitation.objects.filter(
                uuid=self.project_group_invitation.uuid
            ).exists()
        )
        self.assertFalse(
            models.PermissionRequest.objects.filter(
                uuid=permission_request.uuid
            ).exists()
        )


@ddt
class GroupInvitationUpdateTest(BaseGroupInvitationTest):
    def _get_update_url(self, invitation):
        return factories.GroupInvitationBaseFactory.get_url(invitation)

    @data("staff", "customer_owner")
    def test_user_with_access_can_update_project_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.patch(
            self._get_update_url(self.project_group_invitation),
            data={"auto_approve": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_group_invitation.refresh_from_db()
        self.assertTrue(self.project_group_invitation.auto_approve)

    @data("staff", "customer_owner")
    def test_user_with_access_can_update_customer_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.patch(
            self._get_update_url(self.customer_group_invitation),
            data={"auto_approve": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer_group_invitation.refresh_from_db()
        self.assertTrue(self.customer_group_invitation.auto_approve)

    def test_staff_can_set_is_public(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        # Use a project-scoped invitation with auto_create_project for public
        factories.ProjectGroupInvitationFactory(
            scope=self.project,
            auto_create_project=False,
        )
        # First make it a customer-scoped invitation with project role and auto_create_project
        invitation_customer = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            role=ProjectRole.ADMIN,
            auto_create_project=True,
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self._get_update_url(invitation_customer),
            data={"is_public": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invitation_customer.refresh_from_db()
        self.assertTrue(invitation_customer.is_public)

    def test_non_staff_cannot_set_is_public(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            role=ProjectRole.ADMIN,
            auto_create_project=True,
        )
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.patch(
            self._get_update_url(invitation),
            data={"is_public": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_update_inactive_invitation(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.project_group_invitation.cancel()
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self._get_update_url(self.project_group_invitation),
            data={"auto_approve": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("project_admin", "user")
    def test_user_without_permission_gets_404(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.patch(
            self._get_update_url(self.project_group_invitation),
            data={"auto_approve": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_invitation_requires_auto_create_project(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            role=ProjectRole.ADMIN,
            auto_create_project=True,
            is_public=True,
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self._get_update_url(invitation),
            data={"auto_create_project": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_partial_update_single_field(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self._get_update_url(self.project_group_invitation),
            data={"user_email_patterns": [".*@example.com"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_group_invitation.refresh_from_db()
        self.assertEqual(
            self.project_group_invitation.user_email_patterns, [".*@example.com"]
        )

    def test_can_update_role(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self._get_update_url(self.project_group_invitation),
            data={"role": ProjectRole.ADMIN.uuid.hex},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_group_invitation.refresh_from_db()
        self.assertEqual(self.project_group_invitation.role, ProjectRole.ADMIN)

    def test_project_manager_can_update_project_invitation(self):
        self.client.force_authenticate(user=self.project_manager)
        response = self.client.patch(
            self._get_update_url(self.project_group_invitation),
            data={"auto_approve": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_group_invitation.refresh_from_db()
        self.assertTrue(self.project_group_invitation.auto_approve)

    def test_can_update_custom_text(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.staff)
        response = self.client.patch(
            self._get_update_url(self.project_group_invitation),
            data={"custom_text": "Updated custom text"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_group_invitation.refresh_from_db()
        self.assertEqual(
            self.project_group_invitation.custom_text, "Updated custom text"
        )


@ddt
class RequestCreateTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.group_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer
        )
        self.url = factories.CustomerGroupInvitationFactory.get_url(
            self.group_invitation, "submit_request"
        )

    @data("staff", "project_admin", "project_manager", "user")
    def test_create_request(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            models.PermissionRequest.objects.filter(
                invitation=self.group_invitation
            ).exists()
        )

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_with_existing_role_cannot_create_request(self):
        """customer_owner already has CUSTOMER.OWNER role, so should be rejected."""
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @mock.patch(
        "waldur_core.users.handlers.tasks."
        "send_mail_notification_about_permission_request_has_been_submitted.delay"
    )
    def test_notification_about_permission_request_has_been_submitted(
        self, mock_tasks: mock.Mock
    ):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        permission_request = models.PermissionRequest.objects.get(
            invitation=self.group_invitation
        )
        mock_tasks.assert_called_once_with(permission_request.id)


class RequestNotificationRecipientsTest(BaseInvitationTest):
    """Recipient resolution for the permission_request_submitted notification."""

    def setUp(self):
        super().setUp()
        # A customer with no owners holding the create permission, so role-based
        # resolution yields nobody and we fall back to customer-level emails.
        self.orphan_customer = structure_factories.CustomerFactory(
            email="contact@example.com",
            notification_emails="ops@example.com, ops@example.com , second@example.com",
        )
        self.orphan_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.orphan_customer
        )

    def test_get_customer_notification_emails_combines_contact_and_list(self):
        # The helper splits and trims but does not de-duplicate; the task does.
        self.assertEqual(
            utils.get_customer_notification_emails(self.orphan_customer),
            [
                "contact@example.com",
                "ops@example.com",
                "ops@example.com",
                "second@example.com",
            ],
        )

    def test_get_customer_notification_emails_empty(self):
        customer = structure_factories.CustomerFactory(email="", notification_emails="")
        self.assertEqual(utils.get_customer_notification_emails(customer), [])

    @mock.patch("waldur_core.users.tasks.broadcast_mail")
    def test_notification_sent_to_customer_emails_when_no_owners(self, mock_broadcast):
        request = factories.PermissionRequestFactory(invitation=self.orphan_invitation)
        tasks.send_mail_notification_about_permission_request_has_been_submitted(
            request.id
        )
        mock_broadcast.assert_called_once()
        # de-duped, blanks dropped, order preserved
        self.assertEqual(
            mock_broadcast.call_args[0][3],
            ["contact@example.com", "ops@example.com", "second@example.com"],
        )

    @mock.patch("waldur_core.users.tasks.broadcast_mail")
    def test_notification_includes_both_owners_and_customer_emails(
        self, mock_broadcast
    ):
        self.customer.email = "owner-contact@example.com"
        self.customer.save()
        invitation = factories.CustomerGroupInvitationFactory(scope=self.customer)
        request = factories.PermissionRequestFactory(invitation=invitation)
        tasks.send_mail_notification_about_permission_request_has_been_submitted(
            request.id
        )
        emails = mock_broadcast.call_args[0][3]
        self.assertIn(self.customer_owner.email, emails)
        self.assertIn("owner-contact@example.com", emails)

    @mock.patch("waldur_core.users.tasks.broadcast_mail")
    def test_notification_falls_back_to_staff_when_no_recipients(self, mock_broadcast):
        customer = structure_factories.CustomerFactory(email="", notification_emails="")
        invitation = factories.CustomerGroupInvitationFactory(scope=customer)
        request = factories.PermissionRequestFactory(invitation=invitation)
        tasks.send_mail_notification_about_permission_request_has_been_submitted(
            request.id
        )
        mock_broadcast.assert_called_once()
        self.assertIn(self.staff.email, mock_broadcast.call_args[0][3])

    def test_auto_create_project_invitation_notifies_project_creators(self):
        """For an auto_create_project invitation, owners who can create projects
        (but not customers) must be notified, since they are the ones authorized
        to approve. Regression for empty recipient lists caused by resolving
        CREATE_CUSTOMER_PERMISSION from the customer scope."""
        customer = structure_factories.CustomerFactory(email="", notification_emails="")
        customer_ct = ContentType.objects.get_for_model(customer)
        # A role that can create projects within a customer but cannot create
        # customers, matching a real deployment's CUSTOMER.OWNER.
        project_creator_role = Role.objects.create(
            name="TEST.PROJECT_CREATOR", content_type=customer_ct
        )
        project_creator_role.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        approver = structure_factories.UserFactory(email="approver@example.com")
        add_user(customer, approver, project_creator_role)

        invitation = factories.CustomerGroupInvitationFactory(
            scope=customer, auto_create_project=True
        )
        request = factories.PermissionRequestFactory(invitation=invitation)

        recipients = utils.get_users_for_notification_about_request_has_been_submitted(
            request
        )
        self.assertIn(approver, recipients)

    def test_auto_create_project_invitation_ignores_customer_creators(self):
        """A user who can only create customers is not an approver for an
        auto_create_project invitation and must not be notified."""
        customer = structure_factories.CustomerFactory(email="", notification_emails="")
        customer_ct = ContentType.objects.get_for_model(customer)
        customer_creator_role = Role.objects.create(
            name="TEST.CUSTOMER_CREATOR", content_type=customer_ct
        )
        customer_creator_role.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        outsider = structure_factories.UserFactory(email="outsider@example.com")
        add_user(customer, outsider, customer_creator_role)

        invitation = factories.CustomerGroupInvitationFactory(
            scope=customer, auto_create_project=True
        )
        request = factories.PermissionRequestFactory(invitation=invitation)

        recipients = utils.get_users_for_notification_about_request_has_been_submitted(
            request
        )
        self.assertNotIn(outsider, recipients)


@ddt
class RequestRetrieveTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.customer_group_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer
        )
        self.permission_request = factories.PermissionRequestFactory(
            invitation=self.customer_group_invitation,
        )
        self.url = factories.PermissionRequestFactory.get_url(self.permission_request)
        self.url_list = factories.PermissionRequestFactory.get_list_url()

    @data("staff", "customer_owner")
    def test_user_can_get_request(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(self.url_list)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_permission_request_includes_created_by_email_and_template(self):
        """Test that permission request API response includes created_by_email and project_name_template fields."""
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify the new fields are present in the response
        self.assertIn("created_by_email", response.data)
        self.assertIn("project_name_template", response.data)

        # Verify the values are correct
        self.assertEqual(
            response.data["created_by_email"], self.permission_request.created_by.email
        )

    @data("project_admin", "project_manager", "user")
    def test_user_cannot_get_request(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(self.url_list)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_can_see_requests_submitted_by_himself(self):
        self.client.force_authenticate(user=self.permission_request.created_by)
        response = self.client.get(self.url_list)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@ddt
class RequestApproveTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.customer_group_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer
        )
        self.permission_request = factories.PermissionRequestFactory(
            invitation=self.customer_group_invitation,
        )
        self.url = factories.PermissionRequestFactory.get_url(
            self.permission_request, "approve"
        )
        self.created_by = self.permission_request.created_by

    @data("staff", "customer_owner")
    def test_user_can_approve_request(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.permission_request.refresh_from_db()
        self.assertEqual(self.permission_request.state, ReviewStates.APPROVED)
        self.assertTrue(has_user(self.customer, self.permission_request.created_by))

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data("customer_owner", "created_by")
    def test_user_cannot_approve_request(self, user):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(self.url)
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.permission_request.refresh_from_db()
        self.assertEqual(self.permission_request.state, ReviewStates.PENDING)

    def test_customer_owner_can_approve_project_scoped_request(self):
        project_invitation = factories.ProjectGroupInvitationFactory(scope=self.project)
        permission_request = factories.PermissionRequestFactory(
            invitation=project_invitation
        )
        url = factories.PermissionRequestFactory.get_url(permission_request, "approve")
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permission_request.refresh_from_db()
        self.assertEqual(permission_request.state, ReviewStates.APPROVED)

    def test_owner_with_only_project_create_can_approve_auto_create_request(self):
        """Approving an auto_create_project invitation creates a project and grants a
        PROJECT role, so a customer role holding PROJECT.CREATE_PERMISSION but not
        CUSTOMER.CREATE_PERMISSION must be able to approve it (it is offered the action
        in the UI)."""
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        # CREATE_PROJECT_PERMISSION stays granted on CustomerRole.OWNER (see setUp).
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_create_project=True,
            project_role=ProjectRole.ADMIN,
            project_name_template="{username}_project",
        )
        permission_request = factories.PermissionRequestFactory(invitation=invitation)
        url = factories.PermissionRequestFactory.get_url(permission_request, "approve")
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permission_request.refresh_from_db()
        self.assertEqual(permission_request.state, ReviewStates.APPROVED)

    def test_approve_rejects_mismatched_role_availability(self):
        """A role limited by RoleAvailability to a different scope must not
        be granted via PermissionRequest.approve."""
        from django.contrib.contenttypes.models import ContentType

        from waldur_core.permissions.models import RoleAvailability

        other_customer = structure_factories.CustomerFactory()
        customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
        RoleAvailability.objects.create(
            role=self.customer_group_invitation.role,
            content_type=customer_ct,
            object_id=other_customer.id,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not available", str(response.data).lower())
        self.assertFalse(has_user(self.customer, self.permission_request.created_by))
        self.permission_request.refresh_from_db()
        # State already moved to APPROVED by super().approve(); the rollback
        # comes from @transaction.atomic on PermissionRequest.approve.
        self.assertEqual(self.permission_request.state, ReviewStates.PENDING)


@ddt
class RequestRejectTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.customer_group_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer
        )
        self.permission_request = factories.PermissionRequestFactory(
            invitation=self.customer_group_invitation,
        )
        self.url = factories.PermissionRequestFactory.get_url(
            self.permission_request, "reject"
        )

    @data("staff", "customer_owner")
    def test_user_can_reject_request(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.permission_request.refresh_from_db()
        self.assertEqual(self.permission_request.state, ReviewStates.REJECTED)

    def test_owner_with_only_project_create_can_reject_auto_create_request(self):
        """Mirror of the approve case: rejecting an auto_create_project request must be
        available to a customer role with PROJECT.CREATE_PERMISSION but not
        CUSTOMER.CREATE_PERMISSION."""
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_create_project=True,
            project_role=ProjectRole.ADMIN,
            project_name_template="{username}_project",
        )
        permission_request = factories.PermissionRequestFactory(invitation=invitation)
        url = factories.PermissionRequestFactory.get_url(permission_request, "reject")
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permission_request.refresh_from_db()
        self.assertEqual(permission_request.state, ReviewStates.REJECTED)

    def test_customer_owner_can_reject_project_scoped_request(self):
        project_invitation = factories.ProjectGroupInvitationFactory(scope=self.project)
        permission_request = factories.PermissionRequestFactory(
            invitation=project_invitation
        )
        url = factories.PermissionRequestFactory.get_url(permission_request, "reject")
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        permission_request.refresh_from_db()
        self.assertEqual(permission_request.state, ReviewStates.REJECTED)

    @override_settings(task_always_eager=True)
    def test_rejection_notifies_requester(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        structure_factories.NotificationFactory(key="users.permission_request_rejected")
        self.permission_request.created_by.email = "requester@example.com"
        self.permission_request.created_by.save(update_fields=["email"])

        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(self.url, {"comment": "Not eligible"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["requester@example.com"])
        self.assertIn("rejected", mail.outbox[0].subject.lower())


@ddt
class RequestDestroyTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.customer_group_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer
        )
        self.permission_request = factories.PermissionRequestFactory(
            invitation=self.customer_group_invitation,
        )
        self.url = factories.PermissionRequestFactory.get_url(self.permission_request)

    def test_staff_can_delete_permission_request(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.PermissionRequest.objects.filter(
                pk=self.permission_request.pk
            ).exists()
        )

    def test_customer_owner_cannot_delete_permission_request(self):
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.PermissionRequest.objects.filter(
                pk=self.permission_request.pk
            ).exists()
        )

    @data("project_admin", "project_manager", "user")
    def test_user_without_scope_access_cannot_delete_permission_request(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(
            models.PermissionRequest.objects.filter(
                pk=self.permission_request.pk
            ).exists()
        )

    def test_anonymous_cannot_delete_permission_request(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class PermissionRequestProjectCreationTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.user_with_template = structure_factories.UserFactory(
            username="template_user",
            email="template@example.com",
            first_name="Template",
            last_name="User",
        )

    def test_create_project_with_template(self):
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_create_project=True,
            project_role=ProjectRole.ADMIN,
            project_name_template="{username}_custom_project",
        )

        permission_request = factories.PermissionRequestFactory(
            invitation=invitation, created_by=self.user_with_template
        )

        permission_request.approve(self.staff)

        # Check that project was created with template name
        self.assertTrue(
            structure_models.Project.objects.filter(
                name="template_user_custom_project", customer=self.customer
            ).exists()
        )

    def test_create_project_without_template_uses_default(self):
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_create_project=True,
            project_role=ProjectRole.ADMIN,
            project_name_template="",
        )

        permission_request = factories.PermissionRequestFactory(
            invitation=invitation, created_by=self.user_with_template
        )

        permission_request.approve(self.staff)

        # Check that project was created with username (default)
        self.assertTrue(
            structure_models.Project.objects.filter(
                name="template_user", customer=self.customer
            ).exists()
        )

    def test_valid_project_name_template_placeholders(self):
        """Test that valid placeholders in project_name_template are accepted."""
        self.client.force_authenticate(user=self.staff)

        # Test each valid placeholder
        valid_templates = [
            "{username}_project",
            "{email}_workspace",
            "{full_name} Research",
            "Project for {username} - {email}",
        ]

        for template in valid_templates:
            response = self.client.post(
                factories.CustomerGroupInvitationFactory.get_list_url(),
                {
                    "scope": structure_factories.CustomerFactory.get_url(self.customer),
                    "role": CustomerRole.OWNER.uuid.hex,
                    "auto_create_project": True,
                    "project_role": ProjectRole.ADMIN.uuid.hex,
                    "project_name_template": template,
                },
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_201_CREATED,
                f"Template '{template}' should be valid. Response: {response.data}",
            )

    def test_invalid_project_name_template_placeholders_rejected(self):
        """Test that invalid placeholders in project_name_template are rejected."""
        self.client.force_authenticate(user=self.staff)

        # Test invalid placeholders
        invalid_templates = [
            "{user}_project",  # Legacy placeholder not supported
            "{invalid_placeholder}_workspace",
            "{user.username}_research",
            "Project {user.full_name}",
        ]

        for template in invalid_templates:
            response = self.client.post(
                factories.CustomerGroupInvitationFactory.get_list_url(),
                {
                    "scope": structure_factories.CustomerFactory.get_url(self.customer),
                    "role": CustomerRole.OWNER.uuid.hex,
                    "auto_create_project": True,
                    "project_role": ProjectRole.ADMIN.uuid.hex,
                    "project_name_template": template,
                },
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
                f"Template '{template}' should be invalid",
            )
            self.assertIn("project_name_template", response.data)

    def test_invalid_template_fallback_to_username(self):
        """Test that if an invalid template somehow gets through, it falls back to username."""
        # Directly create an invitation with invalid template (bypassing validation)
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_create_project=True,
            project_role=ProjectRole.ADMIN,
            project_name_template="{invalid_var}_project",
        )

        # Manually set the template to bypass validation
        invitation.project_name_template = "{invalid_var}_project"
        invitation.save()

        permission_request = factories.PermissionRequestFactory(
            invitation=invitation, created_by=self.user_with_template
        )

        # Approve should not crash, but fall back to username
        permission_request.approve(self.staff)

        # Check that project was created with fallback username
        self.assertTrue(
            structure_models.Project.objects.filter(
                name="template_user", customer=self.customer
            ).exists()
        )

    def test_approve_returns_created_project(self):
        """approve() returns the project and created flag for downstream consumers."""
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_create_project=True,
            project_role=ProjectRole.ADMIN,
            project_name_template="{username}_project",
        )
        permission_request = factories.PermissionRequestFactory(
            invitation=invitation, created_by=self.user_with_template
        )

        result = permission_request.approve(self.staff)

        self.assertIsNotNone(result)
        self.assertTrue(result["project_created"])
        self.assertEqual(result["project"].name, "template_user_project")
        self.assertEqual(result["project"].customer, self.customer)

    def test_approve_returns_reused_project(self):
        """approve() reports project_created=False when get_or_create matched."""
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_create_project=True,
            project_role=ProjectRole.ADMIN,
            project_name_template="{username}_project",
        )
        existing = structure_factories.ProjectFactory(
            customer=self.customer, name="template_user_project"
        )
        permission_request = factories.PermissionRequestFactory(
            invitation=invitation, created_by=self.user_with_template
        )

        result = permission_request.approve(self.staff)

        self.assertFalse(result["project_created"])
        self.assertEqual(result["project"], existing)

    def test_approve_without_auto_create_project_returns_no_project(self):
        """approve() returns project=None when invitation has no auto_create_project."""
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_create_project=False,
            role=CustomerRole.OWNER,
        )
        permission_request = factories.PermissionRequestFactory(
            invitation=invitation, created_by=self.user_with_template
        )

        result = permission_request.approve(self.staff)

        self.assertIsNotNone(result)
        self.assertIsNone(result["project"])
        self.assertFalse(result["project_created"])


class GroupInvitationPatternTest(BaseGroupInvitationTest):
    def setUp(self):
        super().setUp()
        self.invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            user_email_patterns=[".*@example.com", "test@.*"],
            user_affiliations=["staff", "student"],
        )
        self.url = factories.CustomerGroupInvitationFactory.get_url(
            self.invitation, "submit_request"
        )

    def test_user_with_matching_email_can_submit_request(self):
        user = structure_factories.UserFactory(email="user@example.com")
        self.client.force_authenticate(user=user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_with_matching_affiliation_can_submit_request(self):
        user = structure_factories.UserFactory(affiliations=["staff"])
        self.client.force_authenticate(user=user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_with_non_matching_email_and_affiliation_cannot_submit_request(self):
        user = structure_factories.UserFactory(
            email="user@other.com",
            affiliations=["other"],
        )
        self.client.force_authenticate(user=user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_submit_request_if_no_patterns_defined(self):
        invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            user_email_patterns=[],
            user_affiliations=[],
        )
        url = factories.CustomerGroupInvitationFactory.get_url(
            invitation, "submit_request"
        )
        user = structure_factories.UserFactory(
            email="any@email.com",
            affiliations=[],
        )
        self.client.force_authenticate(user=user)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_with_matching_email_pattern_can_submit_request(self):
        user = structure_factories.UserFactory(email="test@domain.com")
        self.client.force_authenticate(user=user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_with_no_affiliations_cannot_submit_request_if_affiliations_required(
        self,
    ):
        user = structure_factories.UserFactory(
            email="user@other.com",
        )
        self.client.force_authenticate(user=user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GroupInvitationAutoApprovalTest(BaseGroupInvitationTest):
    def setUp(self):
        super().setUp()
        self.auto_approve_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_approve=True,
            user_email_patterns=[".*@example.com"],
            user_affiliations=["staff"],
        )
        self.url = factories.CustomerGroupInvitationFactory.get_url(
            self.auto_approve_invitation, "submit_request"
        )

    def test_matching_user_request_is_auto_approved(self):
        """Test that permission requests from matching users are automatically approved."""
        user = structure_factories.UserFactory(email="user@example.com")
        self.client.force_authenticate(user=user)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that the response indicates auto-approval
        self.assertTrue(response.data["auto_approved"])

        # Check that the permission request was created and approved
        permission_request = models.PermissionRequest.objects.get(
            invitation=self.auto_approve_invitation, created_by=user
        )
        self.assertEqual(permission_request.state, ReviewStates.APPROVED)

        # Check that user has been granted the role
        self.assertTrue(
            has_user(self.customer, user, self.auto_approve_invitation.role)
        )

    def test_matching_user_by_affiliation_request_is_auto_approved(self):
        """Test that permission requests from users with matching affiliations are auto-approved."""
        user = structure_factories.UserFactory(
            email="other@different.com", affiliations=["staff"]
        )
        self.client.force_authenticate(user=user)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that the response indicates auto-approval
        self.assertTrue(response.data["auto_approved"])

        # Check that the permission request was created and approved
        permission_request = models.PermissionRequest.objects.get(
            invitation=self.auto_approve_invitation, created_by=user
        )
        self.assertEqual(permission_request.state, ReviewStates.APPROVED)

        # Check that user has been granted the role
        self.assertTrue(
            has_user(self.customer, user, self.auto_approve_invitation.role)
        )

    def test_auto_approval_disabled_requires_manual_approval(self):
        """Test that when auto_approve is False, requests require manual approval."""
        manual_approval_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_approve=False,
            user_email_patterns=[".*@example.com"],
            user_affiliations=["staff"],
        )
        url = factories.CustomerGroupInvitationFactory.get_url(
            manual_approval_invitation, "submit_request"
        )

        user = structure_factories.UserFactory(email="user@example.com")
        self.client.force_authenticate(user=user)

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that the response indicates manual approval is needed
        self.assertFalse(response.data["auto_approved"])

        # Check that the permission request was created but NOT approved
        permission_request = models.PermissionRequest.objects.get(
            invitation=manual_approval_invitation, created_by=user
        )
        self.assertEqual(permission_request.state, ReviewStates.PENDING)

        # Check that user has NOT been granted the role yet
        self.assertFalse(has_user(self.customer, user, manual_approval_invitation.role))

    def test_auto_approval_with_project_creation(self):
        """Test auto-approval works with auto_create_project enabled."""
        project_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_approve=True,
            auto_create_project=True,
            user_email_patterns=[".*@example.com"],
            role=ProjectRole.ADMIN,
            project_role=ProjectRole.ADMIN,
        )
        url = factories.CustomerGroupInvitationFactory.get_url(
            project_invitation, "submit_request"
        )

        user = structure_factories.UserFactory(email="user@example.com")
        self.client.force_authenticate(user=user)

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that the response indicates auto-approval
        self.assertTrue(response.data["auto_approved"])

        # Check that the permission request was created and approved
        permission_request = models.PermissionRequest.objects.get(
            invitation=project_invitation, created_by=user
        )
        self.assertEqual(permission_request.state, ReviewStates.APPROVED)

        # Check that a project was created and user has role in it
        created_project = structure_models.Project.objects.get(
            customer=self.customer, name=user.username
        )
        self.assertTrue(has_user(created_project, user, ProjectRole.ADMIN))

        # New project_uuid / project_created fields are populated for downstream routing
        self.assertEqual(response.data["project_uuid"], created_project.uuid.hex)
        self.assertTrue(response.data["project_created"])

    def test_pending_response_omits_project_fields(self):
        """When approval is required, project_uuid and project_created are null."""
        manual_approval_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_approve=False,
            user_email_patterns=[".*@example.com"],
        )
        url = factories.CustomerGroupInvitationFactory.get_url(
            manual_approval_invitation, "submit_request"
        )

        user = structure_factories.UserFactory(email="user@example.com")
        self.client.force_authenticate(user=user)

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["auto_approved"])
        self.assertIsNone(response.data["project_uuid"])
        self.assertIsNone(response.data["project_created"])

    def test_auto_approval_without_project_creation_omits_project_fields(self):
        """Auto-approve at customer scope (no auto_create_project) -> no project fields."""
        # self.auto_approve_invitation has auto_create_project=False (the default)
        user = structure_factories.UserFactory(email="user@example.com")
        self.client.force_authenticate(user=user)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["auto_approved"])
        self.assertIsNone(response.data["project_uuid"])
        self.assertIsNone(response.data["project_created"])

    def test_auto_approval_reuses_existing_project(self):
        """When a project with the resolved name already exists, it is reused (project_created=False)."""
        project_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_approve=True,
            auto_create_project=True,
            user_email_patterns=[".*@example.com"],
            role=ProjectRole.ADMIN,
            project_role=ProjectRole.ADMIN,
        )
        url = factories.CustomerGroupInvitationFactory.get_url(
            project_invitation, "submit_request"
        )

        user = structure_factories.UserFactory(email="user@example.com")
        # Pre-create the project that would be auto-created.
        existing_project = structure_factories.ProjectFactory(
            customer=self.customer, name=user.username
        )
        self.client.force_authenticate(user=user)

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["auto_approved"])
        self.assertEqual(response.data["project_uuid"], existing_project.uuid.hex)
        self.assertFalse(response.data["project_created"])
        self.assertTrue(has_user(existing_project, user, ProjectRole.ADMIN))


class GroupInvitationDuplicateRolePreventionTest(BaseGroupInvitationTest):
    def setUp(self):
        super().setUp()
        self.auto_approve_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_approve=True,
            user_email_patterns=[".*@example.com"],
        )
        self.url = factories.CustomerGroupInvitationFactory.get_url(
            self.auto_approve_invitation, "submit_request"
        )

    def test_auto_approved_user_cannot_submit_duplicate_request(self):
        """Submit twice with auto_approve=True — second request should be rejected."""
        user = structure_factories.UserFactory(email="user@example.com")
        self.client.force_authenticate(user=user)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["auto_approved"])

        # Second submission should be blocked
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify only one role was granted
        role_count = UserRole.objects.filter(
            user=user,
            is_active=True,
            role=self.auto_approve_invitation.role,
        ).count()
        self.assertEqual(role_count, 1)

    def test_user_with_existing_role_cannot_submit_request(self):
        """User who already has the role via other means gets rejected."""
        user = structure_factories.UserFactory(email="user@example.com")
        add_user(self.customer, user, self.auto_approve_invitation.role)

        self.client.force_authenticate(user=user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(INVITATION_DISABLE_MULTIPLE_ROLES=True)
    def test_multiple_roles_disabled_blocks_group_invitation_with_existing_role(self):
        """INVITATION_DISABLE_MULTIPLE_ROLES blocks different role in same scope."""
        user = structure_factories.UserFactory(email="user@example.com")
        # Grant a different role in the same customer scope
        add_user(self.customer, user, CustomerRole.SUPPORT)

        self.client.force_authenticate(user=user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_manual_approve_does_not_create_duplicate_role(self):
        """Approve two requests for same scope — only one role should be created."""
        manual_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_approve=False,
            user_email_patterns=[".*@example.com"],
        )

        user = structure_factories.UserFactory(email="user@example.com")

        # Create two pending permission requests (simulating overlapping group invitations)
        pr1 = models.PermissionRequest.objects.create(
            invitation=self.auto_approve_invitation,
            created_by=user,
        )
        pr1.submit()

        pr2 = models.PermissionRequest.objects.create(
            invitation=manual_invitation,
            created_by=user,
        )
        pr2.submit()

        # Approve both — second should silently skip role creation
        pr1.approve(self.staff)
        pr2.approve(self.staff)

        role_count = UserRole.objects.filter(
            user=user,
            is_active=True,
            role=self.auto_approve_invitation.role,
        ).count()
        self.assertEqual(role_count, 1)

    def test_soft_deleted_project_is_not_reused(self):
        """When auto_create_project=True and project was soft-deleted, a new project is created."""
        project_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer,
            auto_approve=True,
            auto_create_project=True,
            user_email_patterns=[".*@example.com"],
            role=ProjectRole.ADMIN,
            project_role=ProjectRole.ADMIN,
        )
        url = factories.CustomerGroupInvitationFactory.get_url(
            project_invitation, "submit_request"
        )

        user = structure_factories.UserFactory(email="user@example.com")

        # Pre-create a project with the expected name and soft-delete it
        project_name = user.username
        old_project = structure_models.Project.objects.create(
            name=project_name,
            customer=self.customer,
        )
        old_project.delete()  # soft delete
        old_project.refresh_from_db()
        self.assertTrue(old_project.is_removed)

        self.client.force_authenticate(user=user)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # A new non-deleted project should exist
        new_project = structure_models.Project.available_objects.get(
            name=project_name,
            customer=self.customer,
        )
        self.assertNotEqual(new_project.pk, old_project.pk)
