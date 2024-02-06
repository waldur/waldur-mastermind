from datetime import timedelta
from unittest import mock

from ddt import data, ddt
from django.conf import settings
from django.utils import timezone
from rest_framework import status

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import has_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models, tasks
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

    @data("project_admin", "project_manager")
    def test_user_without_access_cannot_create_project_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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
        self, invitation: models.Invitation = None, role: Role = None
    ):
        invitation = invitation or factories.ProjectInvitationFactory.build()
        role = role or ProjectRole.ADMIN
        return {
            "scope": structure_factories.ProjectFactory.get_url(invitation.scope),
            "role": role.uuid.hex,
        }

    def _get_valid_customer_invitation_payload(
        self, invitation: models.Invitation = None, role: Role = None
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

    @data("project_admin", "project_manager", "user")
    def test_user_without_access_cannot_cancel_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation, action="cancel"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

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
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_invitation_is_canceled_after_expiration_date(self):
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section["GROUP_INVITATION_LIFETIME"] = timedelta(weeks=1)

        with self.settings(WALDUR_CORE=waldur_section):
            invitation = factories.ProjectGroupInvitationFactory(
                created=timezone.now() - timedelta(weeks=1)
            )
            tasks.cancel_expired_group_invitations()

        self.assertEqual(
            models.GroupInvitation.objects.get(uuid=invitation.uuid).is_active, False
        )


@ddt
class RequestCreateTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.group_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer
        )
        self.url = factories.CustomerGroupInvitationFactory.get_url(
            self.group_invitation, "request"
        )

    @data("staff", "customer_owner", "project_admin", "project_manager", "user")
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

    @mock.patch("waldur_core.users.handlers.tasks")
    def test_notification_about_permission_request_has_been_submitted(self, mock_tasks):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        permission_request = models.PermissionRequest.objects.get(
            invitation=self.group_invitation
        )
        mock_tasks.send_mail_notification_about_permission_request_has_been_submitted.delay.assert_called_once_with(
            permission_request.id
        )


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
        self.assertEqual(
            self.permission_request.state, models.PermissionRequest.States.APPROVED
        )
        self.assertTrue(has_user(self.customer, self.permission_request.created_by))

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data("customer_owner", "created_by")
    def test_user_cannot_approve_request(self, user):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.permission_request.refresh_from_db()
        self.assertEqual(
            self.permission_request.state, models.PermissionRequest.States.PENDING
        )


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
        self.assertEqual(
            self.permission_request.state, models.PermissionRequest.States.REJECTED
        )

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data("customer_owner")
    def test_user_cannot_reject_request(self, user):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.permission_request.refresh_from_db()
        self.assertEqual(
            self.permission_request.state, models.PermissionRequest.States.PENDING
        )
