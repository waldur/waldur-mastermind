from datetime import timedelta

from ddt import ddt, data
from django.conf import settings
from django.utils import timezone
from freezegun import freeze_time
from mock_django import mock_signal_receiver
from rest_framework import test, status

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure import models as structure_models
from waldur_core.structure import signals
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import models, tasks
from waldur_core.users.tests import factories


class BaseInvitationTest(test.APITransactionTestCase):

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer_owner = structure_factories.UserFactory()
        self.project_admin = structure_factories.UserFactory()
        self.project_manager = structure_factories.UserFactory()
        self.user = structure_factories.UserFactory()

        self.customer = structure_factories.CustomerFactory()
        self.second_customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.customer_owner, structure_models.CustomerRole.OWNER)

        self.customer_role = structure_models.CustomerRole.OWNER
        self.customer_invitation = factories.CustomerInvitationFactory(
            customer=self.customer, customer_role=self.customer_role)

        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.project.add_user(self.project_admin, structure_models.ProjectRole.ADMINISTRATOR)
        self.project.add_user(self.project_manager, structure_models.ProjectRole.MANAGER)

        self.project_role = structure_models.ProjectRole.ADMINISTRATOR
        self.project_invitation = factories.ProjectInvitationFactory(
            project=self.project, project_role=self.project_role)


@ddt
class InvitationPermissionApiTest(BaseInvitationTest):

    # List tests
    def test_user_can_list_invitations(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(factories.InvitationBaseFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # Retrieve tests
    @data('staff', 'customer_owner')
    def test_user_with_access_can_retrieve_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(factories.ProjectInvitationFactory.get_url(self.project_invitation))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_retrieve_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(factories.ProjectInvitationFactory.get_url(self.project_invitation))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data('staff', 'customer_owner')
    def test_user_with_access_can_retrieve_customer_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(factories.CustomerInvitationFactory.get_url(self.customer_invitation))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_retrieve_customer_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.get(factories.CustomerInvitationFactory.get_url(self.customer_invitation))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Creation tests
    @data('staff', 'customer_owner')
    def test_user_with_access_can_create_project_admin_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_invitation, project_role=structure_models.ProjectRole.ADMINISTRATOR)
        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('staff', 'customer_owner')
    def test_user_with_access_can_create_project_manager_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(
            self.project_invitation, project_role=structure_models.ProjectRole.MANAGER)
        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_owner_can_create_project_manager_invitation_if_settings_are_tweaked(self):
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_project_invitation_payload(
            self.project_invitation, project_role=structure_models.ProjectRole.MANAGER)
        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_create_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data, {'detail': 'You do not have permission to perform this action.'})

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('staff', 'customer_owner')
    def test_user_with_access_can_create_customer_owner_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_owner_can_not_create_customer_owner_invitation_if_settings_are_tweaked(self):
        self.client.force_authenticate(user=self.customer_owner)
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('staff', 'customer_owner')
    def test_user_which_created_invitation_is_stored_in_inviatation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        invitation = models.Invitation.objects.get(uuid=response.data['uuid'])
        self.assertEqual(invitation.created_by, getattr(self, user))

    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_create_customer_owner_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data, {'detail': 'You do not have permission to perform this action.'})

    @data('staff', 'customer_owner')
    def test_user_with_access_can_cancel_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(factories.ProjectInvitationFactory.get_url(self.project_invitation,
                                                                               action='cancel'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.state, models.Invitation.State.CANCELED)

    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_cancel_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(factories.ProjectInvitationFactory.get_url(self.project_invitation,
                                                                               action='cancel'))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('staff', 'customer_owner')
    def test_user_with_access_can_cancel_customer_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(factories.CustomerInvitationFactory.get_url(self.customer_invitation,
                                                                                action='cancel'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer_invitation.refresh_from_db()
        self.assertEqual(self.customer_invitation.state, models.Invitation.State.CANCELED)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_owner_can_not_cancel_customer_invitation_if_settings_are_tweaked(self):
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(factories.CustomerInvitationFactory.get_url(self.customer_invitation,
                                                                                action='cancel'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=True)
    @data('staff', 'customer_owner')
    def test_user_with_access_can_send_customer_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(factories.CustomerInvitationFactory.get_url(self.customer_invitation,
                                                                                action='send'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_waldur_core_settings(OWNERS_CAN_MANAGE_OWNERS=False)
    def test_owner_can_not_send_customer_invitation_if_settings_are_tweaked(self):
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(factories.CustomerInvitationFactory.get_url(self.customer_invitation,
                                                                                action='send'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data('staff', 'customer_owner')
    def test_user_with_access_can_send_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(factories.ProjectInvitationFactory.get_url(self.project_invitation,
                                                                               action='send'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_can_send_project_invitation_if_settings_are_tweaked(self):
        self.client.force_authenticate(user=self.customer_owner)
        response = self.client.post(factories.ProjectInvitationFactory.get_url(self.project_invitation,
                                                                               action='send'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('project_admin', 'project_manager', 'user')
    def test_user_without_access_cannot_send_project_invitation(self, user):
        self.client.force_authenticate(user=getattr(self, user))
        response = self.client.post(factories.ProjectInvitationFactory.get_url(self.project_invitation,
                                                                               action='send'))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # API tests
    def test_user_cannot_create_invitation_with_invalid_link_template(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        payload['link_template'] = '/invalid/link'
        response = self.client.post(factories.ProjectInvitationFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {'link_template': ["Link template must include '{uuid}' parameter."]})

    def test_user_cannot_create_invitation_for_project_and_customer_simultaneously(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        customer_payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        payload['customer'] = customer_payload['customer']
        payload['customer_role'] = customer_payload['customer_role']

        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data,
                         {'non_field_errors': ["Cannot create invitation to project and customer simultaneously."]})

    def test_user_cannot_create_invitation_without_customer_or_project(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        payload.pop('project')

        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {'non_field_errors': ["Customer or project must be provided."]})

    def test_user_cannot_create_project_invitation_without_project_role(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        payload.pop('project_role')

        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {'project_role': ["Project and its role must be provided."]})

    def test_user_cannot_create_customer_invitation_without_customer_role(self):
        self.client.force_authenticate(user=self.staff)
        payload = self._get_valid_customer_invitation_payload(self.customer_invitation)
        payload.pop('customer_role')

        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {'customer_role': ["Customer and its role must be provided."]})

    def test_user_can_create_invitation_for_existing_user(self):
        self.client.force_authenticate(user=self.staff)
        email = 'test@example.com'
        structure_factories.UserFactory(email=email)
        payload = self._get_valid_project_invitation_payload(self.project_invitation)
        payload['email'] = email

        response = self.client.post(factories.InvitationBaseFactory.get_list_url(), data=payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_invitation_is_canceled_after_expiration_date(self):
        waldur_section = settings.WALDUR_CORE.copy()
        waldur_section['INVITATION_LIFETIME'] = timedelta(weeks=1)

        with self.settings(WALDUR_CORE=waldur_section):
            invitation = factories.ProjectInvitationFactory(created=timezone.now() - timedelta(weeks=1))
            tasks.cancel_expired_invitations()

        self.assertEqual(models.Invitation.objects.get(uuid=invitation.uuid).state, models.Invitation.State.EXPIRED)

    def test_filtering_by_customer_uuid_includes_project_invitations_for_that_customer_too(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(factories.InvitationBaseFactory.get_list_url(), {
            'customer': self.customer.uuid.hex
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_filtering_by_customer_url_includes_project_invitations_for_that_customer_too(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(factories.InvitationBaseFactory.get_list_url(), {
            'customer_url': structure_factories.CustomerFactory.get_url(self.customer)
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_filtering_by_another_customer_does_not_includes_project_invitations_for_initial_customer(self):
        other_customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(user=self.staff)
        response = self.client.get(factories.InvitationBaseFactory.get_list_url(), {
            'customer': other_customer.uuid.hex
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    @freeze_time('2018-05-15 00:00:00')
    def test_user_can_resend_expired_invitation(self):
        customer_expired_invitation = factories.CustomerInvitationFactory(
            state=models.Invitation.State.EXPIRED)

        self.client.force_authenticate(user=self.staff)
        response = self.client.post(factories.CustomerInvitationFactory
                                    .get_url(customer_expired_invitation, action='send'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        customer_expired_invitation.refresh_from_db()
        self.assertEqual(customer_expired_invitation.state, models.Invitation.State.PENDING)
        self.assertEqual(customer_expired_invitation.created, timezone.now())

    # Helper methods
    def _get_valid_project_invitation_payload(self, invitation=None, project_role=None):
        invitation = invitation or factories.ProjectInvitationFactory.build()
        return {
            'email': invitation.email,
            'link_template': invitation.link_template,
            'project': structure_factories.ProjectFactory.get_url(invitation.project),
            'project_role': project_role or structure_models.ProjectRole.ADMINISTRATOR,
        }

    def _get_valid_customer_invitation_payload(self, invitation=None, customer_role=None):
        invitation = invitation or factories.CustomerInvitationFactory.build()
        return {
            'email': invitation.email,
            'link_template': invitation.link_template,
            'customer': structure_factories.CustomerFactory.get_url(invitation.customer),
            'customer_role': customer_role or structure_models.CustomerRole.OWNER,
        }


class InvitationAcceptTest(BaseInvitationTest):

    def test_authenticated_user_can_accept_project_invitation(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(factories.ProjectInvitationFactory.get_url(
            self.project_invitation, action='accept'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.project_invitation.refresh_from_db()
        self.assertEqual(self.project_invitation.state, models.Invitation.State.ACCEPTED)
        self.assertTrue(self.project.has_user(self.user, self.project_invitation.project_role))

    def test_authenticated_user_can_accept_customer_invitation(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(factories.CustomerInvitationFactory.get_url(
            self.customer_invitation, action='accept'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.customer_invitation.refresh_from_db()
        self.assertEqual(self.customer_invitation.state, models.Invitation.State.ACCEPTED)
        self.assertTrue(self.customer.has_user(self.user, self.customer_invitation.customer_role))

    def test_user_with_invalid_civil_number_cannot_accept_invitation(self):
        customer_invitation = factories.CustomerInvitationFactory(
            customer=self.customer, customer_role=self.customer_role, civil_number='123456789')
        self.client.force_authenticate(user=self.user)
        response = self.client.post(factories.CustomerInvitationFactory.get_url(customer_invitation, action='accept'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, ['User has an invalid civil number.'])

    def test_user_which_already_has_role_within_customer_cannot_accept_invitation(self):
        customer_invitation = factories.CustomerInvitationFactory(
            customer=self.customer, customer_role=self.customer_role)
        self.client.force_authenticate(user=self.user)
        self.customer.add_user(self.user, customer_invitation.customer_role)
        response = self.client.post(factories.CustomerInvitationFactory.get_url(customer_invitation, action='accept'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, ['User already has role within this customer.'])

    def test_user_which_already_has_role_within_project_cannot_accept_invitation(self):
        project_invitation = factories.ProjectInvitationFactory(
            project=self.project, project_role=self.project_role)
        self.client.force_authenticate(user=self.user)
        self.project.add_user(self.user, project_invitation.project_role)
        response = self.client.post(factories.ProjectInvitationFactory.get_url(project_invitation, action='accept'))

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, ['User already has role within this project.'])

    def test_user_which_created_invitation_is_stored_in_permission(self):
        invitation = factories.CustomerInvitationFactory(created_by=self.customer_owner)
        self.client.force_authenticate(user=self.user)
        self.client.post(factories.CustomerInvitationFactory.get_url(invitation, action='accept'))
        permission = structure_models.CustomerPermission.objects.get(user=self.user, customer=invitation.customer)
        self.assertEqual(permission.created_by, self.customer_owner)

    def test_user_can_rewrite_his_email_on_invitation_accept(self):
        invitation = factories.CustomerInvitationFactory(created_by=self.customer_owner, email='invitation@i.ua')
        self.client.force_authenticate(user=self.user)

        self.client.post(
            factories.CustomerInvitationFactory.get_url(invitation, action='accept'), {'replace_email': True})

        self.assertEqual(self.user.email, invitation.email)

    @override_waldur_core_settings(VALIDATE_INVITATION_EMAIL=True)
    def test_user_can_not_rewrite_his_email_on_acceptance_if_validation_of_emails_is_on(self):
        invitation = factories.CustomerInvitationFactory(created_by=self.customer_owner, email='invitation@i.ua')
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerInvitationFactory.get_url(invitation, action='accept')

        response = self.client.post(url, {'replace_email': True})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.email, invitation.email)

    @override_waldur_core_settings(VALIDATE_INVITATION_EMAIL=False)
    def test_user_can_rewrite_his_email_on_acceptance_if_validation_of_emails_is_off(self):
        invitation = factories.CustomerInvitationFactory(created_by=self.customer_owner, email=self.user.email)
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerInvitationFactory.get_url(invitation, action='accept')

        response = self.client.post(url, {'replace_email': True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, invitation.email)

    @override_waldur_core_settings(VALIDATE_INVITATION_EMAIL=True)
    def test_user_can_accept_invitation_if_emails_match_and_validation_of_emails_is_on(self):
        invitation = factories.CustomerInvitationFactory(created_by=self.customer_owner, email=self.user.email)
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerInvitationFactory.get_url(invitation, action='accept')

        response = self.client.post(url, {'replace_email': True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, invitation.email)

    def test_when_invitation_is_accepted_event_is_emitted(self):
        # Arrange
        self.project_invitation.created_by = self.customer_owner
        self.project_invitation.save()

        # Act
        with mock_signal_receiver(signals.structure_role_granted) as mock_signal:
            self.client.force_authenticate(user=self.user)
            self.client.post(factories.ProjectInvitationFactory.get_url(
                self.project_invitation, action='accept'))

            # Assert
            mock_signal.assert_called_once_with(
                structure=self.project,
                user=self.user,
                role=self.project_role,

                sender=structure_models.Project,
                signal=signals.structure_role_granted,
                created_by=self.customer_owner
            )
