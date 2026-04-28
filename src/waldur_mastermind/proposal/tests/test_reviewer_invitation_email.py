"""Tests for reviewer pool invitation email sending."""

from django.core import mail
from django.test import override_settings
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import tasks

from . import factories


@override_settings(task_always_eager=True)
class InviteByEmailSendsEmailTest(test.APITestCase):
    """Test that invite-by-email endpoint sends an invitation email."""

    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        self.call_managing_org = self.call.manager
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.call_managing_org.add_user(self.manager, CallRole.MANAGER)
        structure_factories.NotificationFactory(
            key="proposal.reviewer_invitation",
        )

    def test_email_sent_on_invite_by_email(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="invite-by-email"
            ),
            {"email": "newreviewer@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("newreviewer@example.com", mail.outbox[0].to)
        self.assertIn(self.call.name, mail.outbox[0].subject)

    def test_email_contains_invitation_link(self):
        self.client.force_authenticate(self.manager)
        self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="invite-by-email"
            ),
            {"email": "newreviewer@example.com"},
            format="json",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reviewer-invitation", mail.outbox[0].body)

    def test_email_contains_call_name(self):
        self.client.force_authenticate(self.manager)
        self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="invite-by-email"
            ),
            {"email": "newreviewer@example.com"},
            format="json",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.call.name, mail.outbox[0].body)

    def test_email_contains_inviter_name(self):
        self.client.force_authenticate(self.manager)
        self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="invite-by-email"
            ),
            {"email": "newreviewer@example.com"},
            format="json",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.manager.full_name, mail.outbox[0].body)


@override_settings(task_always_eager=True)
class SendReviewerInvitationEmailTaskTest(test.APITestCase):
    """Unit tests for the send_reviewer_invitation_email task."""

    def setUp(self):
        structure_factories.NotificationFactory(
            key="proposal.reviewer_invitation",
        )
        self.inviter = structure_factories.UserFactory()
        self.pool_member = factories.CallReviewerPoolFactory(
            reviewer=None,
            invited_email="reviewer@example.com",
            invited_by=self.inviter,
        )

    def test_sends_email_to_invited_address(self):
        tasks.send_reviewer_invitation_email(self.pool_member.uuid)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reviewer@example.com", mail.outbox[0].to)

    def test_invited_by_null_uses_site_name_fallback(self):
        self.pool_member.invited_by = None
        self.pool_member.save()

        tasks.send_reviewer_invitation_email(self.pool_member.uuid)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reviewer@example.com", mail.outbox[0].to)
