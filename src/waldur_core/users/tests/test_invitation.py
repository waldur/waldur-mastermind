import datetime
from datetime import timedelta
from unittest import mock
from uuid import uuid4

from constance.test.unittest import override_config
from ddt import data, ddt
from django.conf import settings
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.core.enums import ReviewStates
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.logging import models as logging_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole, ProposalRole
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import get_permissions
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models, tasks
from waldur_core.users.enums import InvitationState
from waldur_core.users.tests import factories
from waldur_core.users.utils import get_invitation_link, get_invitation_token
from waldur_mastermind.proposal.tests.factories import ProposalFactory


class InvitationFieldValidationTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer = structure_factories.CustomerFactory()
        self.customer_owner = structure_factories.UserFactory()
        self.customer.add_user(self.customer_owner, CustomerRole.OWNER)

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_INVITATIONS)

    def test_extra_invitation_text_within_limit(self):
        """Test that extra_invitation_text with 2000 characters or less is valid"""
        self.client.force_authenticate(user=self.staff)

        valid_text = "a" * 2000  # Exactly 2000 characters
        payload = {
            "email": "test@example.com",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,
            "extra_invitation_text": valid_text,
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the text was saved correctly
        invitation = models.Invitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.extra_invitation_text, valid_text)

    def test_extra_invitation_text_exceeds_limit(self):
        """Test that extra_invitation_text with more than 2000 characters is invalid"""
        self.client.force_authenticate(user=self.staff)

        invalid_text = "a" * 2001  # 2001 characters - exceeds limit
        payload = {
            "email": "test@example.com",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,
            "extra_invitation_text": invalid_text,
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("extra_invitation_text", response.data)

    def test_extra_invitation_text_empty_is_valid(self):
        """Test that empty extra_invitation_text is valid"""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "email": "test@example.com",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,
            "extra_invitation_text": "",
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the text was saved correctly
        invitation = models.Invitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.extra_invitation_text, "")

    def test_extra_invitation_text_omitted_is_valid(self):
        """Test that omitting extra_invitation_text is valid (defaults to empty)"""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "email": "test@example.com",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Verify the text defaults to empty
        invitation = models.Invitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.extra_invitation_text, "")


class BaseInvitationTest(test.APITransactionTestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_PROJECTS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_PROJECTS)

        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_INVITATIONS)
        ProjectRole.ADMIN.add_permission(PermissionEnum.LIST_INVITATIONS)
        ProjectRole.MANAGER.add_permission(PermissionEnum.LIST_INVITATIONS)

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer_owner = structure_factories.UserFactory()
        self.project_admin = structure_factories.UserFactory()
        self.project_manager = structure_factories.UserFactory()
        self.user = structure_factories.UserFactory()

        self.customer = structure_factories.CustomerFactory()
        self.second_customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.customer_owner, CustomerRole.OWNER)

        self.extra_invitation_text = "invitation text"
        self.customer_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            extra_invitation_text=self.extra_invitation_text,
        )

        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.project.add_user(self.project_admin, ProjectRole.ADMIN)
        self.project.add_user(self.project_manager, ProjectRole.MANAGER)

        self.project_invitation = factories.ProjectInvitationFactory(
            scope=self.project,
            role=ProjectRole.ADMIN,
        )


class InvitationDuplicateCheckTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(user=self.staff)
        self.check_duplicates_url = factories.InvitationBaseFactory.get_list_url(
            action="check-duplicates"
        )

    def test_returns_duplicates_for_pending_invitations(self):
        invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email="dup@example.com",
            state=InvitationState.PENDING,
        )
        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "invitations": [
                {"email": "Dup@example.com", "role": CustomerRole.OWNER.uuid.hex},
                {"email": "unique@example.com", "role": CustomerRole.OWNER.uuid.hex},
                {"email": "dup@example.com", "role": CustomerRole.OWNER.uuid.hex},
            ],
        }

        response = self.client.post(self.check_duplicates_url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "duplicates": [
                    {
                        "email": "Dup@example.com",
                        "role": CustomerRole.OWNER.uuid.hex,
                        "existing_invitation_uuid": str(invitation.uuid),
                    }
                ]
            },
        )

    def test_does_not_return_duplicates_for_other_scope_or_role(self):
        factories.CustomerInvitationFactory(
            scope=self.second_customer,
            role=CustomerRole.OWNER,
            email="dup@example.com",
            state=InvitationState.PENDING,
        )
        factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email="roledup@example.com",
            state=InvitationState.PENDING,
        )
        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "invitations": [
                {"email": "dup@example.com", "role": CustomerRole.OWNER.uuid.hex},
                {"email": "roledup@example.com", "role": CustomerRole.SUPPORT.uuid.hex},
            ],
        }

        response = self.client.post(self.check_duplicates_url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"duplicates": []})

    def test_returns_duplicates_within_request(self):
        payload = {
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "invitations": [
                {"email": "dup@example.com", "role": CustomerRole.OWNER.uuid.hex},
                {"email": "dup@example.com", "role": CustomerRole.OWNER.uuid.hex},
            ],
        }

        response = self.client.post(self.check_duplicates_url, data=payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "duplicates": [
                    {
                        "email": "dup@example.com",
                        "role": CustomerRole.OWNER.uuid.hex,
                        "existing_invitation_uuid": None,
                    }
                ]
            },
        )


@ddt
class InvitationRetrieveTest(BaseInvitationTest):
    def test_unauthorized_user_can_not_list_invitations(self):
        self.project_invitation
        self.client.force_authenticate(user=self.user)
        response = self.client.get(factories.InvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    @data("staff", "customer_owner")
    def test_authorized_user_can_retrieve_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(
            factories.ProjectInvitationFactory.get_url(self.project_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        #  test list
        response = self.client.get(factories.InvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_project_manager_can_retrieve_project_invitation(self):
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.project_manager)
        response = self.client.get(
            factories.ProjectInvitationFactory.get_url(self.project_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        #  test list
        response = self.client.get(factories.InvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["execution_state"],
            models.Invitation.ExecutionState.SCHEDULED,
        )

    def test_unauthorized_user_cannot_retrieve_project_invitation(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(
            factories.ProjectInvitationFactory.get_url(self.project_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        #  test list
        response = self.client.get(factories.InvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    @data("staff", "customer_owner")
    def test_authorized_user_can_retrieve_customer_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(
            factories.CustomerInvitationFactory.get_url(self.customer_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("project_admin", "project_manager", "user")
    def test_unauthorized_user_cannot_retrieve_customer_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(
            factories.CustomerInvitationFactory.get_url(self.customer_invitation)
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_filtering_by_customer_uuid_includes_project_invitations_for_that_customer_too(
        self,
    ):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url(),
            {"customer_uuid": self.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_filtering_by_customer_url_includes_project_invitations_for_that_customer_too(
        self,
    ):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url(),
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
            factories.InvitationBaseFactory.get_list_url(),
            {"customer_uuid": other_customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


class RetrievePendingInvitationDetailsTest(BaseInvitationTest):
    def get_details(self, user, invitation):
        self.client.force_authenticate(user=user)
        return self.client.get(
            factories.CustomerInvitationFactory.get_url(invitation, action="details")
        )

    def test_if_user_has_civil_number_only_matching_invitation_is_shown(self):
        customer_invitation = factories.CustomerInvitationFactory(
            customer=self.customer,
            role=CustomerRole.OWNER,
            civil_number="123456789",
        )
        self.user.civil_number = "123456789"
        self.user.save()
        response = self.get_details(self.user, customer_invitation)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_uuid_exists_in_response(self):
        customer_invitation = factories.CustomerInvitationFactory(
            customer=self.customer,
            role=CustomerRole.OWNER,
        )
        response = self.get_details(self.user, customer_invitation)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(str(response.data["customer_uuid"]), self.customer.uuid.hex)

    def test_if_user_has_civil_number_non_matching_invitation_is_concealed(self):
        customer_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            civil_number="123456789",
        )
        response = self.get_details(self.user, customer_invitation)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_waldur_core_settings(VALIDATE_INVITATION_EMAIL=True)
    def test_if_email_validation_is_enabled_matching_invitation_is_shown(
        self,
    ):
        invitation = factories.CustomerInvitationFactory(
            created_by=self.project_admin, email=self.user.email
        )
        response = self.get_details(self.user, invitation)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_waldur_core_settings(VALIDATE_INVITATION_EMAIL=True)
    def test_if_email_validation_is_enabled_non_matching_invitation_is_concealed(
        self,
    ):
        invitation = factories.CustomerInvitationFactory(created_by=self.customer_owner)
        response = self.get_details(self.user, invitation)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_waldur_core_settings(VALIDATE_INVITATION_EMAIL=False)
    def test_if_email_validation_is_disabled_non_matching_invitation_is_shown(
        self,
    ):
        invitation = factories.CustomerInvitationFactory(created_by=self.customer_owner)
        response = self.get_details(self.user, invitation)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class InvitationRetrieveByEmailTest(BaseInvitationTest):
    def get_list(self, user):
        self.client.force_authenticate(user=user)
        response = self.client.get(factories.InvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_user_can_fetch_pending_invitations_with_matching_email(self):
        pending_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email=self.user.email,
            state=InvitationState.PENDING,
        )
        pending_project_invitation = factories.ProjectInvitationFactory(
            scope=self.project,
            role=ProjectRole.ADMIN,
            email=self.user.email,
            state=InvitationState.PENDING_PROJECT,
        )

        response_data = self.get_list(self.user)
        invitation_uuids = [inv["uuid"] for inv in response_data]
        self.assertIn(str(pending_invitation.uuid), invitation_uuids)
        self.assertIn(str(pending_project_invitation.uuid), invitation_uuids)

        # Verify the invitation details
        invitation_data = next(
            inv for inv in response_data if inv["uuid"] == str(pending_invitation.uuid)
        )
        self.assertEqual(invitation_data["email"], self.user.email)
        self.assertEqual(invitation_data["state"], InvitationState.PENDING)

        project_invitation_data = next(
            inv
            for inv in response_data
            if inv["uuid"] == str(pending_project_invitation.uuid)
        )
        self.assertEqual(project_invitation_data["email"], self.user.email)
        self.assertEqual(
            project_invitation_data["state"], InvitationState.PENDING_PROJECT
        )

    def test_user_cannot_fetch_invitation_with_different_email(self):
        different_email_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email="different@example.com",
            state=InvitationState.PENDING,
        )

        response_data = self.get_list(self.user)

        invitation_uuids = [inv["uuid"] for inv in response_data]
        self.assertNotIn(str(different_email_invitation.uuid), invitation_uuids)

    def test_user_can_fetch_invitation_with_case_insensitive_email_match(self):
        uppercase_email_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email=self.user.email.upper(),
            state=InvitationState.PENDING,
        )

        response_data = self.get_list(self.user)

        # Should include invitation with case-insensitive email match
        invitation_uuids = [inv["uuid"] for inv in response_data]
        self.assertIn(str(uppercase_email_invitation.uuid), invitation_uuids)

    def test_user_cannot_fetch_non_pending_invitation_with_matching_email(self):
        accepted_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email=self.user.email,
            state=InvitationState.ACCEPTED,
        )

        response_data = self.get_list(self.user)

        # Should not include non-pending invitations
        invitation_uuids = [inv["uuid"] for inv in response_data]
        self.assertNotIn(str(accepted_invitation.uuid), invitation_uuids)

    def test_user_cannot_send_pending_invitation_with_matching_email(self):
        pending_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email=self.user.email,
            state=InvitationState.PENDING,
        )

        self.client.force_authenticate(user=self.user)
        url = factories.InvitationBaseFactory.get_url(pending_invitation, action="send")

        response = self.client.post(url)
        self.assertIn(response.status_code, [status.HTTP_404_NOT_FOUND])

    def test_user_cannot_cancel_pending_invitation_with_matching_email(self):
        pending_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email=self.user.email,
            state=InvitationState.PENDING,
        )

        self.client.force_authenticate(user=self.user)
        url = factories.InvitationBaseFactory.get_url(
            pending_invitation, action="cancel"
        )

        response = self.client.post(url)
        self.assertIn(response.status_code, [status.HTTP_404_NOT_FOUND])

        pending_invitation.refresh_from_db()
        self.assertEqual(pending_invitation.state, InvitationState.PENDING)


@ddt
class InvitationCreateTest(BaseInvitationTest):
    @data("staff", "customer_owner")
    def test_authorized_user_can_create_project_admin_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_invitation,
            role=ProjectRole.ADMIN,
        )
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "customer_owner")
    def test_authorized_user_can_create_project_manager_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_invitation, role=ProjectRole.MANAGER
        )
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_owner_can_create_project_manager_invitation(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_project_invitation_payload(
            self.project_invitation, role=ProjectRole.MANAGER
        )
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_project_admin_cannot_create_project_invitation(self):
        self.client.force_authenticate(user=self.project_admin)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        # Users without permission get 404 because invitation filtering prevents them from seeing it
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            response.data,
            {"detail": "Not found."},
        )

    def test_unauthorized_user_cannot_create_project_invitation(self):
        self.client.force_authenticate(user=self.user)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("staff", "customer_owner")
    def test_authorized_user_can_create_customer_owner_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_owner_can_not_create_customer_owner_invitation(
        self,
    ):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        # Users without permission get 404 because invitation filtering prevents them from seeing it
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("staff", "customer_owner")
    def test_user_which_created_invitation_is_stored_in_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        invitation = models.Invitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.created_by, getattr(self, user))

    @data("project_admin", "project_manager")
    def test_unauthorized_user_cannot_create_customer_owner_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        "user",
    )
    def test_user_without_access_cannot_create_customer_owner_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data(
        "project_manager",
    )
    def test_user_can_create_project_invitation(self, user):
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_user_cannot_create_project_invitation_if_he_is_manager_in_another_project(
        self,
    ):
        user = self.project_admin
        another_project = structure_factories.ProjectFactory()
        another_project.add_user(user, ProjectRole.MANAGER)
        self.client.force_authenticate(user=user)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_cannot_create_invitation_without_scope(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        payload.pop("scope")

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"scope": ["This field is required."]})

    def test_user_cannot_create_project_invitation_without_role(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        payload.pop("role")

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"role": ["This field is required."]})

    def test_user_cannot_create_duplicate_invitation(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        existing = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            email="dup@example.com",
            state=InvitationState.PENDING,
        )
        payload = {
            "email": existing.email,
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)

    def test_user_cannot_create_customer_invitation_without_role(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        payload.pop("role")

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"role": ["This field is required."]})

    def test_user_can_create_invitation_for_existing_user(self):
        self.client.force_authenticate(user=self.staff)
        email = "test@example.com"
        structure_factories.UserFactory(email=email)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        payload["email"] = email

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(ONLY_STAFF_CAN_INVITE_USERS=True)
    def test_if_only_staff_can_create_invitation_then_owner_creates_invitation_request(
        self,
    ):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        invitation = models.Invitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.state, InvitationState.REQUESTED)

    @data("customer_owner", "staff")
    def test_staff_and_owner_can_pass_extra_invitation_text(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        payload["extra_invitation_text"] = self.extra_invitation_text
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data["extra_invitation_text"], self.extra_invitation_text
        )

    @data("project_manager")
    def test_manager_can_pass_extra_invitation_text(self, user):
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        payload["extra_invitation_text"] = self.extra_invitation_text
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data["extra_invitation_text"], self.extra_invitation_text
        )

    def test_state_is_pending_project_if_project_has_start_date(self):
        self.client.force_authenticate(user=self.staff)
        self.project_invitation.scope.start_date = (
            datetime.date.today() + datetime.timedelta(weeks=1)
        )
        self.project_invitation.scope.save()
        payload = self._get_valid_project_invitation_payload(
            self.project_invitation,
            role=ProjectRole.ADMIN,
        )
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["state"], InvitationState.PENDING_PROJECT)

    def test_proposal_creator_can_create_invitation(self):
        ProposalRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL)
        proposal = ProposalFactory()
        proposal.add_user(proposal.created_by, ProposalRole.MANAGER)
        scope = ProposalFactory.get_url(proposal)
        self.client.force_authenticate(user=proposal.created_by)
        payload = self._get_valid_proposal_invitation_payload(scope)
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # Helper methods
    def _get_valid_project_invitation_payload(
        self, invitation: models.Invitation | None = None, role: Role | None = None
    ):
        invitation = invitation or factories.ProjectInvitationFactory.build()
        email = f"invite-{uuid4().hex}@example.com"
        role = role or ProjectRole.ADMIN
        return {
            "email": email,
            "scope": structure_factories.ProjectFactory.get_url(invitation.scope),
            "role": role.uuid.hex,
        }

    def _get_valid_customer_invitation_payload(
        self, invitation: models.Invitation | None = None, role: Role | None = None
    ):
        invitation = invitation or factories.CustomerInvitationFactory.build()
        email = f"invite-{uuid4().hex}@example.com"
        role = role or CustomerRole.OWNER
        return {
            "email": email,
            "scope": structure_factories.CustomerFactory.get_url(invitation.scope),
            "role": role.uuid.hex,
        }

    def _get_valid_proposal_invitation_payload(self, scope):
        email = "john@doe.com"
        role = ProposalRole.MEMBER
        return {
            "email": email,
            "scope": scope,
            "role": role.uuid.hex,
        }


class InvitationReminderTest(BaseInvitationTest):
    @override_config(HOMEPORT_URL="TEST")
    def test_send_reminder_for_pending_invitations(self):
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section["INVITATION_LIFETIME"] = timedelta(weeks=1)
        waldur_section["TRANSLATION_DOMAIN"] = "TEST"
        event_type = "invitation_created"
        structure_factories.NotificationFactory(key=f"users.{event_type}")

        with self.settings(WALDUR_CORE=waldur_section):
            factories.ProjectInvitationFactory(
                created=timezone.now()
                - waldur_section["INVITATION_LIFETIME"]
                + timedelta(days=1),
                created_by=self.project_admin,
            )
            tasks.send_reminder_for_pending_invitations()

        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue("REMINDER" in mail.outbox[0].subject)
        expected_sender = self.project_admin.full_name
        self.assertIn(expected_sender, mail.outbox[0].body)
        self.assertNotIn(self.project_admin.email, mail.outbox[0].body)

    @override_config(HOMEPORT_URL="TEST")
    def test_send_reminder_uses_username_when_inviter_has_no_full_name(self):
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section["INVITATION_LIFETIME"] = timedelta(weeks=1)
        waldur_section["TRANSLATION_DOMAIN"] = "TEST"
        event_type = "invitation_created"
        structure_factories.NotificationFactory(key=f"users.{event_type}")

        inviter = structure_factories.UserFactory(
            first_name="",
            last_name="",
            username="svc_inviter_01",
            email="",
        )

        with self.settings(WALDUR_CORE=waldur_section):
            factories.ProjectInvitationFactory(
                created=timezone.now()
                - waldur_section["INVITATION_LIFETIME"]
                + timedelta(days=1),
                created_by=inviter,
            )
            tasks.send_reminder_for_pending_invitations()

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("svc_inviter_01", mail.outbox[0].body)
        self.assertNotIn("inviter-not-in-body@example.com", mail.outbox[0].body)

    @override_config(HOMEPORT_URL="TEST")
    def test_send_reminder_uses_email_when_inviter_has_no_full_name(self):
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section["INVITATION_LIFETIME"] = timedelta(weeks=1)
        waldur_section["TRANSLATION_DOMAIN"] = "TEST"
        event_type = "invitation_created"
        structure_factories.NotificationFactory(key=f"users.{event_type}")

        inviter = structure_factories.UserFactory(
            first_name="",
            last_name="",
            username="svc_inviter_01",
            email="inviter-in-body@example.com",
        )

        with self.settings(WALDUR_CORE=waldur_section):
            factories.ProjectInvitationFactory(
                created=timezone.now()
                - waldur_section["INVITATION_LIFETIME"]
                + timedelta(days=1),
                created_by=inviter,
            )
            tasks.send_reminder_for_pending_invitations()

        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("svc_inviter_01", mail.outbox[0].body)
        self.assertIn("inviter-in-body@example.com", mail.outbox[0].body)

    @freeze_time("2025-02-07")
    @override_config(HOMEPORT_URL="TEST")
    def test_send_reminder_uses_project_start_date(self):
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section["INVITATION_LIFETIME"] = timedelta(days=7)
        waldur_section["TRANSLATION_DOMAIN"] = "TEST"
        event_type = "invitation_created"
        structure_factories.NotificationFactory(key=f"users.{event_type}")

        self.project.start_date = datetime.date(2025, 2, 1)
        self.project.save()

        with self.settings(WALDUR_CORE=waldur_section):
            factories.ProjectInvitationFactory(
                scope=self.project,
                state=InvitationState.PENDING,
                created=timezone.now() - timedelta(days=40),
                created_by=self.project_admin,
            )
            tasks.send_reminder_for_pending_invitations()

        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue("REMINDER" in mail.outbox[0].subject)


class InvitationEmailRestrictionTest(test.APITestCase):
    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)

    def test_invitation_blocked_when_email_does_not_match_customer_pattern(self):
        self.customer.user_email_patterns = [r".*@example\.com$"]
        self.customer.save()

        self.client.force_authenticate(user=self.staff)
        payload = {
            "email": "user@gmail.com",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,
        }
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)

    def test_invitation_allowed_when_email_matches_customer_pattern(self):
        self.customer.user_email_patterns = [r".*@example\.com$"]
        self.customer.save()

        self.client.force_authenticate(user=self.staff)
        payload = {
            "email": "user@example.com",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,
        }
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_invitation_blocked_by_parent_customer_pattern_for_project_scope(self):
        self.customer.user_email_patterns = [r".*@example\.com$"]
        self.customer.save()

        self.client.force_authenticate(user=self.staff)
        payload = {
            "email": "user@gmail.com",
            "scope": structure_factories.ProjectFactory.get_url(self.project),
            "role": ProjectRole.ADMIN.uuid.hex,
        }
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)

    def test_invitation_allowed_when_no_patterns_configured(self):
        self.client.force_authenticate(user=self.staff)
        payload = {
            "email": "anyone@anywhere.com",
            "scope": structure_factories.CustomerFactory.get_url(self.customer),
            "role": CustomerRole.OWNER.uuid.hex,
        }
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class InvitationCancelTest(BaseInvitationTest):
    @data("staff", "customer_owner", "project_manager")
    def test_authorized_user_can_cancel_project_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action="cancel"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.state, InvitationState.CANCELED)

    @data("project_admin", "user")
    def test_user_without_access_cannot_cancel_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action="cancel"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("staff", "customer_owner")
    def test_authorized_user_can_cancel_customer_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                self.customer_invitation, action="cancel"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer_invitation.refresh_from_db()
        self.assertEqual(self.customer_invitation.state, InvitationState.CANCELED)

    def test_owner_can_not_cancel_customer_invitation(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                self.customer_invitation, action="cancel"
            )
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invitation_is_canceled_after_expiration_date(self):
        event_type = "invitation_expired"
        structure_factories.NotificationFactory(key=f"users.{event_type}")
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section["INVITATION_LIFETIME"] = timedelta(weeks=1)

        with self.settings(WALDUR_CORE=waldur_section):
            invitation = factories.ProjectInvitationFactory(
                created=timezone.now() - timedelta(weeks=1),
                created_by=self.project_admin,
            )
            tasks.cancel_expired_invitations(models.Invitation.objects.all())

        self.assertEqual(
            models.Invitation.objects.get(uuid=invitation.uuid).state,
            InvitationState.EXPIRED,
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue("expired" in mail.outbox[0].subject)

    @freeze_time("2025-01-10")
    def test_invitation_with_future_project_start_does_not_expire_early(self):
        event_type = "invitation_expired"
        structure_factories.NotificationFactory(key=f"users.{event_type}")
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section["INVITATION_LIFETIME"] = timedelta(days=7)

        self.project.start_date = datetime.date(2025, 2, 1)
        self.project.save()

        with self.settings(WALDUR_CORE=waldur_section):
            invitation = factories.ProjectInvitationFactory(
                scope=self.project,
                state=InvitationState.PENDING_PROJECT,
                created=timezone.now() - timedelta(days=20),
                created_by=self.project_admin,
            )
            tasks.cancel_expired_invitations(models.Invitation.objects.all())

        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.PENDING_PROJECT)

    @freeze_time("2025-02-09")
    def test_invitation_expires_after_project_start_date_lifetime(self):
        event_type = "invitation_expired"
        structure_factories.NotificationFactory(key=f"users.{event_type}")
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section["INVITATION_LIFETIME"] = timedelta(days=7)

        self.project.start_date = datetime.date(2025, 2, 1)
        self.project.save()

        with self.settings(WALDUR_CORE=waldur_section):
            invitation = factories.ProjectInvitationFactory(
                scope=self.project,
                state=InvitationState.PENDING_PROJECT,
                created=timezone.now() - timedelta(days=40),
                created_by=self.project_admin,
            )
            tasks.cancel_expired_invitations(models.Invitation.objects.all())

        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.EXPIRED)


@ddt
class InvitationSendTest(BaseInvitationTest):
    @data("staff", "customer_owner")
    @override_settings(task_always_eager=True)
    def test_authorized_user_can_send_customer_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                self.customer_invitation, action="send"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.customer_invitation.refresh_from_db()
        self.assertEqual(
            self.customer_invitation.execution_state,
            models.Invitation.ExecutionState.OK,
        )

    @override_settings(task_always_eager=True)
    def test_invitation_email_is_rendered_correctly(self):
        event_type = "invitation_created"
        structure_factories.NotificationFactory(key=f"users.{event_type}")
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                self.customer_invitation, action="send"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.customer_invitation.email, mail.outbox[0].to[0])
        link = get_invitation_link(self.customer_invitation.uuid.hex)
        self.assertTrue(link in mail.outbox[0].body)
        self.assertTrue(self.extra_invitation_text in mail.outbox[0].body)

    def test_owner_can_not_send_customer_invitation(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                self.customer_invitation, action="send"
            )
        )
        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("staff", "customer_owner", "project_manager")
    def test_authorized_user_can_send_project_invitation(self, user):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action="send"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_can_send_project_invitation(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION)
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action="send"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("project_admin", "user")
    def test_user_without_access_cannot_send_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action="send"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @freeze_time("2018-05-15")
    def test_user_can_resend_expired_invitation(self):
        customer_expired_invitation = factories.CustomerInvitationFactory(
            state=InvitationState.EXPIRED
        )

        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                customer_expired_invitation, action="send"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        customer_expired_invitation.refresh_from_db()
        self.assertEqual(customer_expired_invitation.state, InvitationState.PENDING)
        self.assertEqual(customer_expired_invitation.created, timezone.now())

    @freeze_time("2018-05-15")
    def test_resend_for_future_project_marks_pending_project(self):
        self.project.start_date = datetime.date(2018, 6, 1)
        self.project.save()
        invitation = factories.ProjectInvitationFactory(
            scope=self.project,
            state=InvitationState.EXPIRED,
            created=timezone.now() - timedelta(days=10),
        )

        self.client.force_authenticate(user=self.staff)
        with mock.patch(
            "waldur_core.users.tasks.send_invitation_created.delay"
        ) as send_invitation:
            response = self.client.post(
                factories.ProjectInvitationFactory.get_url(invitation, action="send")
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.PENDING_PROJECT)
        self.assertEqual(invitation.created, timezone.now())
        send_invitation.assert_not_called()

    @override_settings(task_always_eager=True)
    def test_creating_of_email_log(self):
        structure_factories.NotificationFactory(key="users.invitation_created")
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                self.customer_invitation, action="send"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.customer_invitation.refresh_from_db()
        self.assertTrue(
            logging_models.EmailLog.objects.filter(
                emails=[self.customer_invitation.email],
                subject=f"Invitation to {self.customer_invitation.customer.name} organization",
            ).exists()
        )


class InvitationAcceptTest(BaseInvitationTest):
    def test_authenticated_user_can_accept_project_invitation(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action="accept"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.state, InvitationState.ACCEPTED)
        self.assertTrue(self.project.has_user(self.user, self.project_invitation.role))

    def test_authenticated_user_can_accept_customer_invitation(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                self.customer_invitation, action="accept"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer_invitation.refresh_from_db()
        self.assertEqual(self.customer_invitation.state, InvitationState.ACCEPTED)
        self.assertTrue(
            self.customer.has_user(self.user, self.customer_invitation.role)
        )

    def test_user_with_invalid_civil_number_cannot_accept_invitation(self):
        customer_invitation = factories.CustomerInvitationFactory(
            customer=self.customer,
            role=CustomerRole.OWNER,
            civil_number="123456789",
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                customer_invitation, action="accept"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_which_already_has_role_within_customer_cannot_accept_invitation(self):
        customer_invitation = factories.CustomerInvitationFactory(
            scope=self.customer, role=CustomerRole.OWNER
        )
        self.client.force_authenticate(user=self.user)
        self.customer.add_user(self.user, customer_invitation.role)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                customer_invitation, action="accept"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data, ["User has already the same role in this scope."]
        )

    def test_user_which_already_has_role_within_project_cannot_accept_invitation(self):
        project_invitation = factories.ProjectInvitationFactory(
            scope=self.project,
            role=ProjectRole.ADMIN,
        )
        self.client.force_authenticate(user=self.user)
        self.project.add_user(self.user, project_invitation.role)
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                project_invitation, action="accept"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data, ["User has already the same role in this scope."]
        )

    @override_config(INVITATION_DISABLE_MULTIPLE_ROLES=True)
    def test_user_cannot_have_multiple_roles_in_same_scope_when_disabled(self):
        # Create an invitation for the same customer where customer_owner already has a role
        same_customer_invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.SUPPORT,
        )

        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(
            factories.CustomerInvitationFactory.get_url(
                same_customer_invitation, action="accept"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, ["User already has role within this scope."])

    @override_config(INVITATION_DISABLE_MULTIPLE_ROLES=True)
    def test_user_can_have_roles_in_different_scopes_when_multiple_roles_disabled(self):
        # User with existing role in customer can accept invitation to different project
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action="accept"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.state, InvitationState.ACCEPTED)

    def test_user_which_created_invitation_is_stored_in_permission(self):
        invitation = factories.CustomerInvitationFactory(created_by=self.customer_owner)
        self.client.force_authenticate(user=self.user)
        self.client.post(
            factories.CustomerInvitationFactory.get_url(invitation, action="accept")
        )
        permission = get_permissions(invitation.customer, self.user).get()
        self.assertEqual(permission.created_by, self.customer_owner)

    @override_waldur_core_settings(VALIDATE_INVITATION_EMAIL=True)
    def test_user_can_accept_invitation_if_emails_match_and_validation_of_emails_is_on(
        self,
    ):
        invitation = factories.CustomerInvitationFactory(
            created_by=self.project_admin, email=self.user.email
        )
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerInvitationFactory.get_url(invitation, action="accept")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, invitation.email)

    @override_config(ENABLE_STRICT_CHECK_ACCEPTING_INVITATION=True)
    def test_user_can_not_accept_invitation_if_emails_are_not_equal(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action="accept"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.state, InvitationState.PENDING)

    @override_config(ENABLE_STRICT_CHECK_ACCEPTING_INVITATION=True)
    def test_user_can_accept_invitation_with_different_case_emails(self):
        """Test that a user can accept an invitation if emails match case-insensitively."""
        # Create the invitation with uppercase email
        uppercase_email = self.user.email.upper()
        invitation = factories.CustomerInvitationFactory(
            created_by=self.project_admin, email=uppercase_email
        )
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerInvitationFactory.get_url(invitation, action="accept")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.ACCEPTED)

    @override_config(ENABLE_STRICT_CHECK_ACCEPTING_INVITATION=True)
    def test_user_can_accept_invitation_with_mixed_case_emails(self):
        """Test that a user can accept an invitation if emails match case-insensitively with mixed casing."""
        # Create a user with mixed case email
        mixed_case_user = structure_factories.UserFactory(
            email="MixEd.CaSe@example.com"
        )
        # Create invitation with different case
        invitation = factories.CustomerInvitationFactory(
            created_by=self.project_admin, email="mixed.case@EXAMPLE.com"
        )
        self.client.force_authenticate(user=mixed_case_user)
        url = factories.CustomerInvitationFactory.get_url(invitation, action="accept")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.ACCEPTED)
        self.assertTrue(invitation.scope.has_user(mixed_case_user, invitation.role))

    @override_config(ENABLE_STRICT_CHECK_ACCEPTING_INVITATION=True)
    def test_user_cannot_accept_invitation_with_different_emails_despite_casefolding(
        self,
    ):
        """Test that a user cannot accept an invitation if emails don't match even after casefolding."""
        # Create invitation with completely different email
        invitation = factories.CustomerInvitationFactory(
            created_by=self.project_admin, email="different@example.com"
        )
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerInvitationFactory.get_url(invitation, action="accept")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.PENDING)
        self.assertFalse(invitation.scope.has_user(self.user, invitation.role))
        # Check that the error message is about emails not being equal
        self.assertIn(
            "User’s email and email of the invitation are not equal",
            str(response.data[0]),
        )

    @override_config(ENABLE_STRICT_CHECK_ACCEPTING_INVITATION=False)
    def test_user_can_accept_invitation_with_different_emails_when_strict_check_disabled(
        self,
    ):
        """Test that a user can accept an invitation with different emails if strict checking is disabled."""
        invitation = factories.CustomerInvitationFactory(
            created_by=self.project_admin, email="different@example.com"
        )
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerInvitationFactory.get_url(invitation, action="accept")

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invitation.refresh_from_db()
        self.assertEqual(invitation.state, InvitationState.ACCEPTED)
        self.assertTrue(invitation.scope.has_user(self.user, invitation.role))


class InvitationApproveTest(BaseInvitationTest):
    def test_anonymous_user_can_approve_requested_invitation(self):
        self.project_invitation.state = InvitationState.REQUESTED
        self.project_invitation.save()
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url("approve"),
            {"token": get_invitation_token(self.project_invitation, self.staff)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.state, InvitationState.PENDING)

    def test_anonymous_user_can_not_approve_pending_invitation(self):
        self.project_invitation.state = InvitationState.PENDING
        self.project_invitation.save()
        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url("approve"),
            {"token": get_invitation_token(self.project_invitation, self.staff)},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class InvitationRejectTest(BaseInvitationTest):
    def test_anonymous_user_can_reject_requested_invitation(self):
        self.project_invitation.state = InvitationState.REQUESTED
        self.project_invitation.save()

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url("reject"),
            {"token": get_invitation_token(self.project_invitation, self.staff)},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.state, InvitationState.REJECTED)

    def test_anonymous_user_can_not_reject_rejected_invitation(self):
        self.project_invitation.state = InvitationState.REJECTED
        self.project_invitation.save()

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url("reject"),
            {"token": get_invitation_token(self.project_invitation, self.staff)},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class InvitationScopeDescriptionTest(test.APITestCase):
    """Test cases for the scope_description field in invitation serializer."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer_owner = structure_factories.UserFactory()

        # Create customer with description
        self.customer_with_description = structure_factories.CustomerFactory(
            name="Test Customer", description="This is a customer with description"
        )
        self.customer_with_description.add_user(self.customer_owner, CustomerRole.OWNER)

        # Create customer without description (empty)
        self.customer_without_description = structure_factories.CustomerFactory(
            name="Customer No Desc", description=""
        )

        # Create project with description (projects also have DescribableMixin)
        self.project_with_description = structure_factories.ProjectFactory(
            customer=self.customer_with_description,
            description="This is a project with description",
        )

        # Create project without description (empty)
        self.project_without_description = structure_factories.ProjectFactory(
            customer=self.customer_with_description, description=""
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_INVITATIONS)

    def test_invitation_includes_scope_description_for_customer_with_description(self):
        """Test that invitation for customer with description includes scope_description field."""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "email": "test@example.com",
            "scope": structure_factories.CustomerFactory.get_url(
                self.customer_with_description
            ),
            "role": CustomerRole.OWNER.uuid.hex,
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that scope_description is present and correct
        self.assertIn("scope_description", response.data)
        self.assertEqual(
            response.data["scope_description"], "This is a customer with description"
        )

    def test_invitation_has_empty_scope_description_for_customer_without_description(
        self,
    ):
        """Test that invitation for customer without description has empty scope_description."""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "email": "test@example.com",
            "scope": structure_factories.CustomerFactory.get_url(
                self.customer_without_description
            ),
            "role": CustomerRole.OWNER.uuid.hex,
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that scope_description is present but empty
        self.assertIn("scope_description", response.data)
        self.assertEqual(response.data["scope_description"], "")

    def test_invitation_includes_scope_description_for_project_with_description(self):
        """Test that invitation for project with description includes scope_description field."""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "email": "test@example.com",
            "scope": structure_factories.ProjectFactory.get_url(
                self.project_with_description
            ),
            "role": ProjectRole.ADMIN.uuid.hex,
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that scope_description is present and correct for project
        self.assertIn("scope_description", response.data)
        self.assertEqual(
            response.data["scope_description"], "This is a project with description"
        )

    def test_invitation_has_empty_scope_description_for_project_without_description(
        self,
    ):
        """Test that invitation for project without description has empty scope_description."""
        self.client.force_authenticate(user=self.staff)

        payload = {
            "email": "test@example.com",
            "scope": structure_factories.ProjectFactory.get_url(
                self.project_without_description
            ),
            "role": ProjectRole.ADMIN.uuid.hex,
        }

        response = self.client.post(
            factories.InvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that scope_description is present but empty
        self.assertIn("scope_description", response.data)
        self.assertEqual(response.data["scope_description"], "")

    def test_invitation_list_includes_scope_description(self):
        """Test that invitation list endpoint includes scope_description field."""
        # Create invitations for different scopes
        customer_invitation = factories.CustomerInvitationFactory(
            scope=self.customer_with_description,
            role=CustomerRole.OWNER,
            created_by=self.staff,
        )
        project_invitation = factories.ProjectInvitationFactory(
            scope=self.project_with_description,
            role=ProjectRole.ADMIN,
            created_by=self.staff,
        )

        self.client.force_authenticate(user=self.staff)
        response = self.client.get(factories.InvitationBaseFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 2)

        # Check that all invitations have scope_description field
        for invitation_data in response.data:
            self.assertIn("scope_description", invitation_data)

        # Find our specific invitations and verify descriptions
        customer_inv_data = next(
            (
                inv
                for inv in response.data
                if inv["uuid"] == str(customer_invitation.uuid)
            ),
            None,
        )
        project_inv_data = next(
            (
                inv
                for inv in response.data
                if inv["uuid"] == str(project_invitation.uuid)
            ),
            None,
        )

        if customer_inv_data:
            self.assertEqual(
                customer_inv_data["scope_description"],
                "This is a customer with description",
            )
        if project_inv_data:
            self.assertEqual(
                project_inv_data["scope_description"],
                "This is a project with description",
            )

    def test_invitation_detail_includes_scope_description(self):
        """Test that invitation detail endpoint includes scope_description field."""
        invitation = factories.CustomerInvitationFactory(
            scope=self.customer_with_description,
            role=CustomerRole.OWNER,
            created_by=self.staff,
        )

        self.client.force_authenticate(user=self.staff)
        url = factories.InvitationBaseFactory.get_url(invitation)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("scope_description", response.data)
        self.assertEqual(
            response.data["scope_description"], "This is a customer with description"
        )

    def test_scope_description_updates_when_customer_description_changes(self):
        """Test that scope_description reflects current customer description."""
        invitation = factories.CustomerInvitationFactory(
            scope=self.customer_with_description,
            role=CustomerRole.OWNER,
            created_by=self.staff,
        )

        # Get initial invitation data
        self.client.force_authenticate(user=self.staff)
        url = factories.InvitationBaseFactory.get_url(invitation)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["scope_description"], "This is a customer with description"
        )

        # Update customer description
        self.customer_with_description.description = "Updated customer description"
        self.customer_with_description.save()

        # Get invitation data again
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["scope_description"], "Updated customer description"
        )


class InvitationScopeFilterTest(test.APITestCase):
    """Test cases for scope name and scope description filters in invitation list."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)

        # Create customers with different names and descriptions
        self.customer_alpha = structure_factories.CustomerFactory(
            name="Alpha Customer", description="Alpha description content"
        )
        self.customer_beta = structure_factories.CustomerFactory(
            name="Beta Company", description="Beta description text"
        )
        self.customer_gamma = structure_factories.CustomerFactory(
            name="Gamma Corp", description="Gamma content here"
        )

        # Create projects with different names and descriptions
        self.project_alpha = structure_factories.ProjectFactory(
            name="Alpha Project",
            description="Alpha project description",
            customer=self.customer_alpha,
        )
        self.project_beta = structure_factories.ProjectFactory(
            name="Beta Development",
            description="Beta project content",
            customer=self.customer_beta,
        )

        # Create invitations for different scopes
        self.customer_invitation_alpha = factories.CustomerInvitationFactory(
            scope=self.customer_alpha, created_by=self.staff
        )
        self.customer_invitation_beta = factories.CustomerInvitationFactory(
            scope=self.customer_beta, created_by=self.staff
        )
        self.customer_invitation_gamma = factories.CustomerInvitationFactory(
            scope=self.customer_gamma, created_by=self.staff
        )

        self.project_invitation_alpha = factories.ProjectInvitationFactory(
            scope=self.project_alpha, created_by=self.staff
        )
        self.project_invitation_beta = factories.ProjectInvitationFactory(
            scope=self.project_beta, created_by=self.staff
        )

        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_INVITATIONS)

    def test_filter_by_scope_name_customers(self):
        """Test filtering invitations by customer scope name."""
        self.client.force_authenticate(user=self.staff)

        # Filter by customer name containing "Alpha"
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url() + "?scope_name=Alpha"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return invitations for Alpha Customer and Alpha Project
        invitation_uuids = {inv["uuid"] for inv in response.data}
        expected_uuids = {
            str(self.customer_invitation_alpha.uuid),
            str(self.project_invitation_alpha.uuid),
        }

        self.assertEqual(invitation_uuids, expected_uuids)

    def test_filter_by_scope_name_projects(self):
        """Test filtering invitations by project scope name."""
        self.client.force_authenticate(user=self.staff)

        # Filter by project name containing "Development"
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url() + "?scope_name=Development"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return invitation for Beta Development project
        invitation_uuids = {inv["uuid"] for inv in response.data}
        expected_uuids = {str(self.project_invitation_beta.uuid)}

        self.assertEqual(invitation_uuids, expected_uuids)

    def test_filter_by_scope_description_customers(self):
        """Test filtering invitations by customer scope description."""
        self.client.force_authenticate(user=self.staff)

        # Filter by description containing "content"
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url()
            + "?scope_description=content"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return invitations for Alpha Customer, Gamma Corp, and Beta Project
        invitation_uuids = {inv["uuid"] for inv in response.data}
        expected_uuids = {
            str(self.customer_invitation_alpha.uuid),  # "Alpha description content"
            str(self.customer_invitation_gamma.uuid),  # "Gamma content here"
            str(self.project_invitation_beta.uuid),  # "Beta project content"
        }

        self.assertEqual(invitation_uuids, expected_uuids)

    def test_filter_by_scope_description_projects(self):
        """Test filtering invitations by project scope description."""
        self.client.force_authenticate(user=self.staff)

        # Filter by description containing "project"
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url()
            + "?scope_description=project"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return invitations for both projects
        invitation_uuids = {inv["uuid"] for inv in response.data}
        expected_uuids = {
            str(self.project_invitation_alpha.uuid),  # "Alpha project description"
            str(self.project_invitation_beta.uuid),  # "Beta project content"
        }

        self.assertEqual(invitation_uuids, expected_uuids)

    def test_filter_by_scope_name_case_insensitive(self):
        """Test that scope name filtering is case-insensitive."""
        self.client.force_authenticate(user=self.staff)

        # Filter using lowercase
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url() + "?scope_name=beta"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return invitations for Beta Company and Beta Development
        invitation_uuids = {inv["uuid"] for inv in response.data}
        expected_uuids = {
            str(self.customer_invitation_beta.uuid),
            str(self.project_invitation_beta.uuid),
        }

        self.assertEqual(invitation_uuids, expected_uuids)

    def test_filter_by_scope_description_case_insensitive(self):
        """Test that scope description filtering is case-insensitive."""
        self.client.force_authenticate(user=self.staff)

        # Filter using mixed case
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url() + "?scope_description=TEXT"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return invitation for Beta Company ("Beta description text")
        invitation_uuids = {inv["uuid"] for inv in response.data}
        expected_uuids = {str(self.customer_invitation_beta.uuid)}

        self.assertEqual(invitation_uuids, expected_uuids)

    def test_combined_scope_filters(self):
        """Test using both scope name and description filters together."""
        self.client.force_authenticate(user=self.staff)

        # Filter by name containing "Beta" AND description containing "text"
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url()
            + "?scope_name=Beta&scope_description=text"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return only invitation for Beta Company (has both "Beta" in name and "text" in description)
        invitation_uuids = {inv["uuid"] for inv in response.data}
        expected_uuids = {str(self.customer_invitation_beta.uuid)}

        self.assertEqual(invitation_uuids, expected_uuids)

    def test_filter_no_matches(self):
        """Test filtering with values that don't match any scopes."""
        self.client.force_authenticate(user=self.staff)

        # Filter by non-existent name
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url() + "?scope_name=NonExistent"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_filter_empty_value(self):
        """Test that empty filter values don't filter anything."""
        self.client.force_authenticate(user=self.staff)

        # Filter with empty value
        response = self.client.get(
            factories.InvitationBaseFactory.get_list_url() + "?scope_name="
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should return all invitations
        invitation_uuids = {inv["uuid"] for inv in response.data}
        expected_uuids = {
            str(self.customer_invitation_alpha.uuid),
            str(self.customer_invitation_beta.uuid),
            str(self.customer_invitation_gamma.uuid),
            str(self.project_invitation_alpha.uuid),
            str(self.project_invitation_beta.uuid),
        }

        self.assertEqual(invitation_uuids, expected_uuids)


class GroupInvitationSubmitRequestTest(test.APITestCase):
    """Test cases for the submit_request method response format."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory(name="Test Organization")
        self.project = structure_factories.ProjectFactory(
            customer=self.customer, name="Test Project"
        )

        # Create group invitations for different scopes
        self.customer_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer, is_active=True, is_public=False
        )
        self.project_invitation = factories.ProjectGroupInvitationFactory(
            scope=self.project, is_active=True, is_public=False
        )

    def test_submit_request_returns_uuid_and_scope_name_for_customer(self):
        """Test that submit_request returns UUID, scope name, and scope UUID for customer invitation."""
        self.client.force_authenticate(user=self.user)

        url = factories.GroupInvitationBaseFactory.get_url(
            self.customer_invitation, action="submit_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check response format
        self.assertIn("uuid", response.data)
        self.assertIn("scope_name", response.data)
        self.assertIn("scope_uuid", response.data)

        # Check that scope_name is the customer name
        self.assertEqual(response.data["scope_name"], "Test Organization")

        # Check that scope_uuid is the customer UUID
        self.assertEqual(response.data["scope_uuid"], str(self.customer.uuid))

        # Check that UUID is a valid hex string
        uuid_str = response.data["uuid"]
        self.assertEqual(len(uuid_str), 32)  # UUID hex string length

        # Verify permission request was created
        permission_request = models.PermissionRequest.objects.get(uuid=uuid_str)
        self.assertEqual(permission_request.invitation, self.customer_invitation)
        self.assertEqual(permission_request.created_by, self.user)

    def test_submit_request_returns_uuid_and_scope_name_for_project(self):
        """Test that submit_request returns UUID, scope name, and scope UUID for project invitation."""
        self.client.force_authenticate(user=self.user)

        url = factories.GroupInvitationBaseFactory.get_url(
            self.project_invitation, action="submit_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check response format
        self.assertIn("uuid", response.data)
        self.assertIn("scope_name", response.data)
        self.assertIn("scope_uuid", response.data)

        # Check that scope_name is the project name
        self.assertEqual(response.data["scope_name"], "Test Project")

        # Check that scope_uuid is the project UUID
        self.assertEqual(response.data["scope_uuid"], str(self.project.uuid))

        # Check that UUID is a valid hex string
        uuid_str = response.data["uuid"]
        self.assertEqual(len(uuid_str), 32)  # UUID hex string length

        # Verify permission request was created
        permission_request = models.PermissionRequest.objects.get(uuid=uuid_str)
        self.assertEqual(permission_request.invitation, self.project_invitation)
        self.assertEqual(permission_request.created_by, self.user)

    def test_submit_request_response_schema_compliance(self):
        """Test that the response matches the schema definition."""
        self.client.force_authenticate(user=self.user)

        url = factories.GroupInvitationBaseFactory.get_url(
            self.customer_invitation, action="submit_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify response has exactly the expected fields from SubmitRequestResponseSerializer
        expected_fields = {"uuid", "scope_name", "scope_uuid", "auto_approved"}
        actual_fields = set(response.data.keys())
        self.assertEqual(actual_fields, expected_fields)

        # Verify field types
        self.assertIsInstance(response.data["uuid"], str)
        self.assertIsInstance(response.data["scope_name"], str)
        self.assertIsInstance(response.data["scope_uuid"], str)
        self.assertIsInstance(response.data["auto_approved"], bool)


class PermissionRequestCancelTest(test.APITestCase):
    """Test cases for the cancel_request action."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)

        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)

        # Create group invitations
        self.customer_invitation = factories.CustomerGroupInvitationFactory(
            scope=self.customer, is_active=True
        )
        self.project_invitation = factories.ProjectGroupInvitationFactory(
            scope=self.project, is_active=True
        )

        # Create permission requests
        self.pending_request = models.PermissionRequest.objects.create(
            invitation=self.customer_invitation, created_by=self.user
        )
        self.pending_request.submit()  # Move to PENDING state

        self.draft_request = models.PermissionRequest.objects.create(
            invitation=self.project_invitation, created_by=self.user
        )
        # Keep in DRAFT state

        self.approved_request = models.PermissionRequest.objects.create(
            invitation=self.customer_invitation, created_by=self.user
        )
        self.approved_request.submit()
        self.approved_request.approve(self.staff)  # Move to APPROVED state

    def test_user_can_cancel_own_pending_request(self):
        """Test that a user can cancel their own pending permission request."""
        self.client.force_authenticate(user=self.user)

        url = factories.PermissionRequestFactory.get_url(
            self.pending_request, action="cancel_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check response format (similar to submit_request)
        self.assertIn("uuid", response.data)
        self.assertIn("scope_name", response.data)
        self.assertIn("scope_uuid", response.data)

        # Check response content
        self.assertEqual(response.data["uuid"], self.pending_request.uuid.hex)
        self.assertEqual(response.data["scope_name"], self.customer.name)
        self.assertEqual(response.data["scope_uuid"], str(self.customer.uuid))

    def test_staff_user_can_cancel_other_users_request(self):
        """Test that staff users can cancel any user's permission request."""
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff_user)

        url = factories.PermissionRequestFactory.get_url(
            self.pending_request, action="cancel_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that request was actually canceled
        self.pending_request.refresh_from_db()
        self.assertEqual(self.pending_request.state, ReviewStates.CANCELED)

        # Check response format
        self.assertIn("uuid", response.data)
        self.assertIn("scope_name", response.data)
        self.assertIn("scope_uuid", response.data)

    def test_user_can_cancel_own_draft_request(self):
        """Test that a user can cancel their own draft permission request."""
        self.client.force_authenticate(user=self.user)

        url = factories.PermissionRequestFactory.get_url(
            self.draft_request, action="cancel_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check response format
        self.assertIn("uuid", response.data)
        self.assertIn("scope_name", response.data)
        self.assertIn("scope_uuid", response.data)

        # Check response content for project scope
        self.assertEqual(response.data["uuid"], self.draft_request.uuid.hex)
        self.assertEqual(response.data["scope_name"], self.project.name)
        self.assertEqual(response.data["scope_uuid"], str(self.project.uuid))

        # Verify the request state was changed
        self.draft_request.refresh_from_db()
        self.assertEqual(self.draft_request.state, ReviewStates.CANCELED)

    def test_user_cannot_cancel_other_users_request(self):
        """Test that a user cannot cancel another user's permission request."""
        self.client.force_authenticate(user=self.other_user)

        url = factories.PermissionRequestFactory.get_url(
            self.pending_request, action="cancel_request"
        )
        response = self.client.post(url)

        # Permission request filtering prevents other users from seeing the request at all
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Verify the request state was not changed
        self.pending_request.refresh_from_db()
        self.assertEqual(self.pending_request.state, ReviewStates.PENDING)

    def test_cannot_cancel_approved_request(self):
        """Test that approved requests cannot be canceled."""
        self.client.force_authenticate(user=self.user)

        url = factories.PermissionRequestFactory.get_url(
            self.approved_request, action="cancel_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Only pending or draft", str(response.data))

        # Verify the request state was not changed
        self.approved_request.refresh_from_db()
        self.assertEqual(self.approved_request.state, ReviewStates.APPROVED)

    def test_cannot_cancel_rejected_request(self):
        """Test that rejected requests cannot be canceled."""
        # Create a rejected request
        rejected_request = models.PermissionRequest.objects.create(
            invitation=self.customer_invitation, created_by=self.user
        )
        rejected_request.submit()
        rejected_request.reject(self.staff)  # Move to REJECTED state

        self.client.force_authenticate(user=self.user)

        url = factories.PermissionRequestFactory.get_url(
            rejected_request, action="cancel_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify the request state was not changed
        rejected_request.refresh_from_db()
        self.assertEqual(rejected_request.state, ReviewStates.REJECTED)

    def test_unauthenticated_user_cannot_cancel_request(self):
        """Test that unauthenticated users cannot cancel requests."""
        url = factories.PermissionRequestFactory.get_url(
            self.pending_request, action="cancel_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_cancel_request_twice_fails(self):
        """Test that canceling an already canceled request fails."""
        self.client.force_authenticate(user=self.user)

        url = factories.PermissionRequestFactory.get_url(
            self.pending_request, action="cancel_request"
        )

        # First cancellation should succeed
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Second cancellation should fail
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify the request is still canceled
        self.pending_request.refresh_from_db()
        self.assertEqual(self.pending_request.state, ReviewStates.CANCELED)

    def test_cancel_request_response_schema_compliance(self):
        """Test that the cancel_request response matches the schema definition."""
        self.client.force_authenticate(user=self.user)

        url = factories.PermissionRequestFactory.get_url(
            self.pending_request, action="cancel_request"
        )
        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify response has exactly the expected fields from CancelRequestResponseSerializer
        expected_fields = {"uuid", "scope_name", "scope_uuid"}
        actual_fields = set(response.data.keys())
        self.assertEqual(actual_fields, expected_fields)

        # Verify field types
        self.assertIsInstance(response.data["uuid"], str)
        self.assertIsInstance(response.data["scope_name"], str)
        self.assertIsInstance(response.data["scope_uuid"], str)


class InvitationUpdateTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.invitation = factories.CustomerInvitationFactory(
            scope=self.customer,
            role=CustomerRole.OWNER,
            created_by=self.project_admin,
            email="original@example.com",
        )
        self.project_invitation = factories.ProjectInvitationFactory(
            scope=self.project,
            role=ProjectRole.ADMIN,
            created_by=self.project_admin,
            email="project@example.com",
        )

    def test_customer_owner_can_update_invitation_email(self):
        """Test that customer owner can update invitation email"""
        self.client.force_authenticate(user=self.customer_owner)
        new_email = "updated@example.com"

        url = factories.CustomerInvitationFactory.get_url(self.invitation)
        payload = {"email": new_email}

        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.email, new_email)

    def test_customer_owner_can_update_invitation_role_within_same_scope(self):
        """Test that customer owner can update role within the same scope type"""
        self.client.force_authenticate(user=self.customer_owner)

        url = factories.CustomerInvitationFactory.get_url(self.invitation)
        payload = {"role": CustomerRole.SUPPORT.uuid.hex}

        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.role, CustomerRole.SUPPORT)

    def test_cannot_update_role_to_different_scope_type(self):
        """Test that role cannot be changed to incompatible scope type"""
        self.client.force_authenticate(user=self.customer_owner)

        url = factories.CustomerInvitationFactory.get_url(self.invitation)
        payload = {
            "role": ProjectRole.ADMIN.uuid.hex
        }  # Project role for customer invitation

        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "Role and scope should belong to the same content type", str(response.data)
        )

    def test_cannot_update_accepted_invitation(self):
        """Test that accepted invitations cannot be updated"""
        self.invitation.state = InvitationState.ACCEPTED
        self.invitation.save()

        self.client.force_authenticate(user=self.customer_owner)

        url = factories.CustomerInvitationFactory.get_url(self.invitation)
        payload = {"email": "new@example.com"}

        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Only pending invitations can be edited", str(response.data))

    def test_cannot_update_canceled_invitation(self):
        """Test that canceled invitations cannot be updated"""
        self.invitation.state = InvitationState.CANCELED
        self.invitation.save()

        self.client.force_authenticate(user=self.customer_owner)

        url = factories.CustomerInvitationFactory.get_url(self.invitation)
        payload = {"email": "new@example.com"}

        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Only pending invitations can be edited", str(response.data))

    def test_user_without_permission_cannot_update_invitation(self):
        """Test that user without management permission cannot update invitation"""
        self.client.force_authenticate(user=self.user)

        url = factories.CustomerInvitationFactory.get_url(self.invitation)
        payload = {"email": "new@example.com"}

        response = self.client.patch(url, payload)

        # Users without permission get 404 because invitation filtering prevents them from seeing it
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_can_update_any_invitation(self):
        """Test that staff users can update any invitation"""
        self.client.force_authenticate(user=self.staff)
        new_email = "staff-updated@example.com"

        url = factories.CustomerInvitationFactory.get_url(self.invitation)
        payload = {"email": new_email}

        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.email, new_email)

    def test_project_manager_can_update_project_invitation(self):
        """Test that project manager can update project invitations"""
        self.client.force_authenticate(user=self.project_manager)
        new_email = "project-updated@example.com"

        url = factories.ProjectInvitationFactory.get_url(self.project_invitation)
        payload = {"email": new_email}

        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.email, new_email)

    def test_project_admin_cannot_update_project_invitation(self):
        """Test that project admin cannot update project invitations"""
        self.client.force_authenticate(user=self.project_admin)
        new_email = "project-updated@example.com"

        url = factories.ProjectInvitationFactory.get_url(self.project_invitation)
        payload = {"email": new_email}

        response = self.client.patch(url, payload)

        # Users without permission get 404 because invitation existence is hidden
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_multiple_fields_at_once(self):
        """Test that multiple fields can be updated in a single request"""
        self.client.force_authenticate(user=self.customer_owner)
        new_email = "multi-updated@example.com"

        url = factories.CustomerInvitationFactory.get_url(self.invitation)
        payload = {
            "email": new_email,
            "role": CustomerRole.SUPPORT.uuid.hex,
        }

        response = self.client.patch(url, payload)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.email, new_email)
        self.assertEqual(self.invitation.role, CustomerRole.SUPPORT)


class IsPermanentWebhookErrorTest(test.APITestCase):
    def test_returns_true_for_4xx_errors(self):
        """4xx HTTP errors are permanent and should not be retried."""
        test_cases = [
            'Invitation sending has failed: 400, {"error": "Bad request"}',
            "Invitation sending has failed: 401, Unauthorized",
            "Invitation sending has failed: 403, Forbidden",
            "Invitation sending has failed: 404, Not Found",
            'Invitation sending has failed: 409, {"errorType":"error","message":"User is disabled"}',
            "Invitation sending has failed: 410, Gone",
            "Invitation sending has failed: 422, Unprocessable Entity",
        ]
        for error_message in test_cases:
            with self.subTest(error_message=error_message):
                self.assertTrue(tasks.is_permanent_webhook_error(error_message))

    def test_returns_false_for_5xx_errors(self):
        """5xx HTTP errors are temporary and should be retried."""
        test_cases = [
            "Invitation sending has failed: 500, Internal Server Error",
            "Invitation sending has failed: 502, Bad Gateway",
            "Invitation sending has failed: 503, Service Unavailable",
            "Invitation sending has failed: 504, Gateway Timeout",
        ]
        for error_message in test_cases:
            with self.subTest(error_message=error_message):
                self.assertFalse(tasks.is_permanent_webhook_error(error_message))

    def test_returns_false_for_empty_or_none(self):
        """Empty or None error messages should not be treated as permanent errors."""
        self.assertFalse(tasks.is_permanent_webhook_error(""))
        self.assertFalse(tasks.is_permanent_webhook_error(None))

    def test_returns_false_for_non_http_errors(self):
        """Non-HTTP error messages should not be treated as permanent errors."""
        test_cases = [
            "Connection refused",
            "Unable to fetch access token",
            "Network timeout",
            "SMTP error",
        ]
        for error_message in test_cases:
            with self.subTest(error_message=error_message):
                self.assertFalse(tasks.is_permanent_webhook_error(error_message))


class InvitationResendStuckTaskTest(test.APITestCase):
    def setUp(self):
        self.sender = structure_factories.UserFactory()

    def test_reconciles_pending_scheduled_invitation(self):
        invitation = factories.ProjectInvitationFactory(
            state=InvitationState.PENDING,
            execution_state=models.Invitation.ExecutionState.SCHEDULED,
            created_by=self.sender,
        )

        with mock.patch("waldur_core.users.tasks.process_invitation") as process:
            tasks.resend_stuck_invitations()

            process.delay.assert_called_once_with(
                invitation.uuid.hex,
                self.sender.full_name or self.sender.username,
            )

    def test_reconciles_pending_erred_invitation(self):
        invitation = factories.ProjectInvitationFactory(
            state=InvitationState.PENDING,
            execution_state=models.Invitation.ExecutionState.ERRED,
            created_by=self.sender,
        )

        with mock.patch("waldur_core.users.tasks.process_invitation") as process:
            tasks.resend_stuck_invitations()
            process.delay.assert_called_once_with(
                invitation.uuid.hex,
                self.sender.full_name or self.sender.username,
            )

    def test_does_not_resend_invitation_in_ok_state(self):
        factories.ProjectInvitationFactory(
            state=InvitationState.PENDING,
            execution_state=models.Invitation.ExecutionState.OK,
            created_by=self.sender,
        )

        with mock.patch("waldur_core.users.tasks.process_invitation") as process:
            tasks.resend_stuck_invitations()
            process.delay.assert_not_called()

    def test_skips_expired_invitations(self):
        factories.ProjectInvitationFactory(
            state=InvitationState.PENDING,
            execution_state=models.Invitation.ExecutionState.SCHEDULED,
            created_by=self.sender,
            created=timezone.now()
            - settings.WALDUR_CORE["INVITATION_LIFETIME"]
            - timedelta(days=1),
        )

        with mock.patch("waldur_core.users.tasks.process_invitation") as process:
            tasks.resend_stuck_invitations()
            process.delay.assert_not_called()

    def test_skips_invitations_with_permanent_webhook_errors(self):
        """Invitations with 4xx HTTP errors from webhooks should not be retried."""
        factories.ProjectInvitationFactory(
            state=InvitationState.PENDING,
            execution_state=models.Invitation.ExecutionState.ERRED,
            created_by=self.sender,
            error_message='Invitation sending has failed: 409, {"errorType":"error","message":"User with email test@example.com is disabled","data":null}',
        )

        with mock.patch("waldur_core.users.tasks.process_invitation") as process:
            tasks.resend_stuck_invitations()
            process.delay.assert_not_called()

    def test_retries_invitations_with_temporary_webhook_errors(self):
        """Invitations with 5xx HTTP errors from webhooks should be retried."""
        invitation = factories.ProjectInvitationFactory(
            state=InvitationState.PENDING,
            execution_state=models.Invitation.ExecutionState.ERRED,
            created_by=self.sender,
            error_message="Invitation sending has failed: 500, Internal Server Error",
        )

        with mock.patch("waldur_core.users.tasks.process_invitation") as process:
            tasks.resend_stuck_invitations()
            process.delay.assert_called_once_with(
                invitation.uuid.hex,
                self.sender.full_name or self.sender.username,
            )


class InvitationWebhookScopeTest(test.APITestCase):
    """Tests for webhook scope filtering - webhooks only support project invitations."""

    def setUp(self):
        self.sender = structure_factories.UserFactory()

    @override_waldur_core_settings(
        INVITATION_USE_WEBHOOKS=True,
        INVITATION_WEBHOOK_URL="https://example.com/webhook",
    )
    def test_project_invitation_uses_webhook_when_enabled(self):
        """Project invitations should be sent via webhook when webhooks are enabled."""
        invitation = factories.ProjectInvitationFactory(
            state=InvitationState.PENDING,
            created_by=self.sender,
        )

        with mock.patch("waldur_core.users.utils.post_invitation_to_url") as mock_post:
            tasks.send_invitation_created(invitation.uuid.hex, self.sender.full_name)
            mock_post.assert_called_once()

    @override_waldur_core_settings(INVITATION_USE_WEBHOOKS=True)
    def test_customer_invitation_falls_back_to_email_when_webhooks_enabled(self):
        """Customer invitations should fall back to email even when webhooks are enabled."""
        invitation = factories.CustomerInvitationFactory(
            state=InvitationState.PENDING,
            created_by=self.sender,
        )

        with (
            mock.patch(
                "waldur_core.users.utils.post_invitation_to_url"
            ) as mock_webhook,
            mock.patch("waldur_core.users.tasks.broadcast_mail") as mock_email,
        ):
            tasks.send_invitation_created(invitation.uuid.hex, self.sender.full_name)
            mock_webhook.assert_not_called()
            mock_email.assert_called_once()

    @override_waldur_core_settings(INVITATION_USE_WEBHOOKS=False)
    def test_project_invitation_uses_email_when_webhooks_disabled(self):
        """Project invitations should use email when webhooks are disabled."""
        invitation = factories.ProjectInvitationFactory(
            state=InvitationState.PENDING,
            created_by=self.sender,
        )

        with (
            mock.patch(
                "waldur_core.users.utils.post_invitation_to_url"
            ) as mock_webhook,
            mock.patch("waldur_core.users.tasks.broadcast_mail") as mock_email,
        ):
            tasks.send_invitation_created(invitation.uuid.hex, self.sender.full_name)
            mock_webhook.assert_not_called()
            mock_email.assert_called_once()
