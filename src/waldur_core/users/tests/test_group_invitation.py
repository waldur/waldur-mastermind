from datetime import timedelta

from ddt import data, ddt
from django.conf import settings
from django.utils import timezone
from rest_framework import status

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models, tasks
from waldur_core.users.tests import factories

from .test_invitation import BaseInvitationTest


class BaseGroupInvitationTest(BaseInvitationTest):
    def setUp(self):
        super().setUp()
        self.customer_group_invitation = factories.CustomerGroupInvitationFactory(
            customer=self.customer, customer_role=self.customer_role
        )

        self.project_group_invitation = factories.ProjectGroupInvitationFactory(
            project=self.project, project_role=self.project_role
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

    @data('staff', 'customer_owner', 'project_admin', 'project_manager', 'user')
    def test_authorized_user_can_retrieve_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('staff', 'customer_owner', 'project_admin', 'project_manager', 'user')
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
            {'customer': self.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_filtering_by_customer_url_includes_project_invitations_for_that_customer_too(
        self,
    ):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_list_url(),
            {
                'customer_url': structure_factories.CustomerFactory.get_url(
                    self.customer
                )
            },
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
            {'customer': other_customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_user_can_not_list_projects_of_project_group_invitation(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(
                self.project_group_invitation, action='projects'
            )
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_user_can_list_projects_of_customers_group_invitation(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(
            factories.GroupInvitationBaseFactory.get_url(
                self.customer_group_invitation, action='projects'
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@ddt
class GroupInvitationCreateTest(BaseGroupInvitationTest):
    @data('staff', 'customer_owner')
    def test_user_with_access_can_create_project_admin_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation,
            project_role=structure_models.ProjectRole.ADMINISTRATOR,
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('staff', 'customer_owner')
    def test_user_with_access_can_create_project_manager_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation,
            project_role=structure_models.ProjectRole.MANAGER,
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_owner_can_create_project_manager_invitation_if_settings_are_tweaked(self):
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation,
            project_role=structure_models.ProjectRole.MANAGER,
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_create_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data,
            {'detail': 'You do not have permission to perform this action.'},
        )

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('staff', 'customer_owner')
    def test_user_with_access_can_create_customer_owner_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_owner_can_not_create_customer_owner_invitation_if_settings_are_tweaked(
        self,
    ):
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('staff', 'customer_owner')
    def test_user_which_created_invitation_is_stored_in_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        invitation = models.GroupInvitation.objects.get(uuid=response.data['uuid'])
        self.assertEqual(invitation.created_by, getattr(self, user))

    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_create_customer_owner_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data,
            {'detail': 'You do not have permission to perform this action.'},
        )

    def test_user_cannot_create_invitation_for_project_and_customer_simultaneously(
        self,
    ):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        customer_payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        payload['customer'] = customer_payload['customer']
        payload['customer_role'] = customer_payload['customer_role']

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data,
            {
                'non_field_errors': [
                    "Cannot create invitation to project and customer simultaneously."
                ]
            },
        )

    def test_user_cannot_create_invitation_without_customer_or_project(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        payload.pop('project')

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data,
            {'non_field_errors': ["Customer or project must be provided."]},
        )

    def test_user_cannot_create_project_invitation_without_project_role(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(
            self.project_group_invitation
        )
        payload.pop('project_role')

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data, {'project_role': ["Project and its role must be provided."]}
        )

    def test_user_cannot_create_customer_invitation_without_customer_role(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        payload.pop('customer_role')

        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data,
            {'customer_role': ["Customer and its role must be provided."]},
        )

    @override_waldur_core_settings(
        OWNERS_CAN_MANAGE_OWNERS=True, ONLY_STAFF_CAN_INVITE_USERS=True
    )
    def test_if_only_staff_can_create_invitation_then_owner_creates_invitation_request(
        self,
    ):
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_customer_invitation_payload(
            self.customer_group_invitation
        )
        response = self.client.post(
            factories.GroupInvitationBaseFactory.get_list_url(), data=payload
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        invitation = models.GroupInvitation.objects.get(uuid=response.data['uuid'])
        self.assertEqual(invitation.is_active, True)

    # Helper methods
    def _get_valid_project_invitation_payload(self, invitation=None, project_role=None):
        invitation = invitation or factories.ProjectInvitationFactory.build()
        return {
            'project': structure_factories.ProjectFactory.get_url(invitation.project),
            'project_role': project_role or structure_models.ProjectRole.ADMINISTRATOR,
        }

    def _get_valid_customer_invitation_payload(
        self, invitation=None, customer_role=None
    ):
        invitation = invitation or factories.CustomerInvitationFactory.build()
        return {
            'customer': structure_factories.CustomerFactory.get_url(
                invitation.customer
            ),
            'customer_role': customer_role or structure_models.CustomerRole.OWNER,
        }


@ddt
class GroupInvitationCancelTest(BaseGroupInvitationTest):
    @data('staff', 'customer_owner')
    def test_user_with_access_can_cancel_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation, action='cancel'
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_group_invitation.refresh_from_db()
        self.assertEqual(self.project_group_invitation.is_active, False)

    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_cancel_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.ProjectGroupInvitationFactory.get_url(
                self.project_group_invitation, action='cancel'
            )
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('staff', 'customer_owner')
    def test_user_with_access_can_cancel_customer_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(
            factories.CustomerGroupInvitationFactory.get_url(
                self.customer_group_invitation, action='cancel'
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer_group_invitation.refresh_from_db()
        self.assertEqual(self.customer_group_invitation.is_active, False)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_owner_can_not_cancel_customer_invitation_if_settings_are_tweaked(self):
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(
            factories.CustomerGroupInvitationFactory.get_url(
                self.customer_group_invitation, action='cancel'
            )
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_invitation_is_canceled_after_expiration_date(self):
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section['GROUP_INVITATION_LIFETIME'] = timedelta(weeks=1)

        with self.settings(WALDUR_CORE=waldur_section):
            invitation = factories.ProjectGroupInvitationFactory(
                created=timezone.now() - timedelta(weeks=1)
            )
            tasks.cancel_expired_group_invitations()

        self.assertEqual(
            models.GroupInvitation.objects.get(uuid=invitation.uuid).is_active, False
        )
