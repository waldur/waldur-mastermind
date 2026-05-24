"""Permission and visibility regression tests for reviewer pool invitations.

Covers two related bugs:

* Call managers (CALL.MANAGER, scoped to the Call) could not send reviewer
  pool invitations because the action permissions only checked the
  CallManagingOrganisation scope, which only the call organiser role holds.
* Call organisers (CUSTOMER.CALL_ORGANIZER, scoped to the managing
  organisation) could not see already-sent invitations because the list
  queryset only matched call managers and customer members.

The roles below are granted exactly as in production: CALL.MANAGER only on the
Call, CUSTOMER.CALL_ORGANIZER only on the CallManagingOrganisation. Granting
them more broadly (e.g. also on the customer) masks both bugs.
"""

from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.enums import ReviewerPoolInvitationStatuses

from . import factories


def get_call_organizer_role():
    return Role.objects.get_system_role(
        "CUSTOMER.CALL_ORGANIZER",
        content_type=ContentType.objects.get_for_model(
            proposal_models.CallManagingOrganisation
        ),
    )


@override_settings(task_always_eager=True)
class CallManagerCanSendInvitationsTest(test.APITestCase):
    """A CALL.MANAGER scoped only to the Call can manage the reviewer pool."""

    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        # Role is granted ONLY on the call, not on the managing organisation,
        # mirroring how CALL.MANAGER is assigned in production.
        self.call_manager = structure_factories.UserFactory()
        self.call.add_user(self.call_manager, CallRole.MANAGER)
        structure_factories.NotificationFactory(key="proposal.reviewer_invitation")

    def test_call_manager_can_invite_by_email(self):
        self.client.force_authenticate(self.call_manager)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="invite-by-email"
            ),
            {"email": "newreviewer@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            proposal_models.CallReviewerPool.objects.filter(
                call=self.call, invited_email="newreviewer@example.com"
            ).exists()
        )

    def test_call_manager_can_list_reviewer_pool(self):
        pool_entry = factories.CallReviewerPoolFactory(call=self.call)
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(
            factories.CallFactory.get_protected_url(self.call, action="reviewer-pool"),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pool_uuids = [str(entry["uuid"]) for entry in response.data]
        self.assertIn(str(pool_entry.uuid), pool_uuids)

    def test_unrelated_user_cannot_invite_by_email(self):
        """Users without access to the call cannot see it, so they get 404."""
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="invite-by-email"
            ),
            {"email": "newreviewer@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_settings(task_always_eager=True)
class CallOrganiserCanManagePoolTest(test.APITestCase):
    """A CALL_ORGANIZER scoped only to the managing org can send and see invitations."""

    def setUp(self):
        self.organizer_role = get_call_organizer_role()
        self.organizer_role.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        # Role is granted ONLY on the managing organisation, not the customer,
        # mirroring how CALL_ORGANIZER is assigned in production.
        self.organizer = structure_factories.UserFactory()
        self.call.manager.add_user(self.organizer, self.organizer_role)
        structure_factories.NotificationFactory(key="proposal.reviewer_invitation")
        self.pool_entry = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

    def test_organiser_sees_sent_invitations(self):
        self.client.force_authenticate(self.organizer)
        response = self.client.get(factories.CallReviewerPoolFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pool_uuids = [str(entry["uuid"]) for entry in response.data]
        self.assertIn(str(self.pool_entry.uuid), pool_uuids)

    def test_organiser_can_invite_by_email(self):
        self.client.force_authenticate(self.organizer)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="invite-by-email"
            ),
            {"email": "newreviewer@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_unrelated_user_does_not_see_invitations(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        response = self.client.get(factories.CallReviewerPoolFactory.get_list_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)
