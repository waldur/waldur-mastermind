"""Tests for reviewer pool invitation email sending."""

from django.core import mail
from django.test import override_settings
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import (
    ReviewerPoolInvitationStatuses,
    ReviewerSuggestionStatuses,
)

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

    def test_sends_email_to_reviewer_without_invited_email(self):
        reviewer = factories.ReviewerProfileFactory()
        pool_member = factories.CallReviewerPoolFactory(
            reviewer=reviewer, invited_email=""
        )

        tasks.send_reviewer_invitation_email(pool_member.uuid)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [reviewer.user.email])

    def test_invited_by_null_uses_site_name_fallback(self):
        self.pool_member.invited_by = None
        self.pool_member.save()

        tasks.send_reviewer_invitation_email(self.pool_member.uuid)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reviewer@example.com", mail.outbox[0].to)


@override_settings(task_always_eager=True)
class SendInvitationsToSuggestionsTest(test.APITestCase):
    """Invitations sent to confirmed suggestions get a link and an email."""

    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.call.manager.add_user(self.manager, CallRole.MANAGER)
        structure_factories.NotificationFactory(
            key="proposal.reviewer_invitation",
        )
        self.reviewers = [factories.ReviewerProfileFactory() for _ in range(2)]
        for reviewer in self.reviewers:
            models.ReviewerSuggestion.objects.create(
                call=self.call,
                reviewer=reviewer,
                affinity_score=0.8,
                status=ReviewerSuggestionStatuses.CONFIRMED,
            )

    def send_invitations(self):
        self.client.force_authenticate(self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                factories.CallFactory.get_protected_url(
                    self.call, action="send-invitations"
                )
            )

    def test_each_invitation_gets_its_own_token(self):
        response = self.send_invitations()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["invitations_sent"], 2)
        tokens = set(
            models.CallReviewerPool.objects.filter(call=self.call).values_list(
                "invitation_token", flat=True
            )
        )
        self.assertEqual(len(tokens), 2)
        self.assertNotIn("", tokens)

    def test_email_sent_to_each_reviewer(self):
        self.send_invitations()

        recipients = sorted(m.to[0] for m in mail.outbox)
        self.assertEqual(recipients, sorted(r.user.email for r in self.reviewers))
        self.assertTrue(all("reviewer-invitation/" in m.body for m in mail.outbox))

    def test_repeated_request_invites_nobody_twice(self):
        self.send_invitations()
        mail.outbox.clear()

        response = self.send_invitations()

        self.assertEqual(response.data["invitations_sent"], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_reviewer_already_invited_by_email_is_skipped(self):
        factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_email=self.reviewers[0].user.email.upper(),
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

        response = self.send_invitations()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["invitations_sent"], 1)
        self.assertFalse(
            models.CallReviewerPool.objects.filter(
                call=self.call, reviewer=self.reviewers[0]
            ).exists()
        )
        self.assertEqual([m.to for m in mail.outbox], [[self.reviewers[1].user.email]])


@override_settings(task_always_eager=True)
class InviteReviewersToPoolSendsEmailTest(test.APITestCase):
    """Inviting existing reviewer profiles to the pool sends an email."""

    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.call.manager.add_user(self.manager, CallRole.MANAGER)
        structure_factories.NotificationFactory(
            key="proposal.reviewer_invitation",
        )
        self.reviewer = factories.ReviewerProfileFactory()

    def invite(self):
        self.client.force_authenticate(self.manager)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                factories.CallFactory.get_protected_url(
                    self.call, action="reviewer-pool"
                ),
                {"reviewer_uuids": [self.reviewer.uuid.hex]},
                format="json",
            )

    def test_email_sent_to_reviewer(self):
        response = self.invite()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.reviewer.user.email])

    def test_reviewer_already_invited_by_email_is_skipped(self):
        factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_email=self.reviewer.user.email,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

        response = self.invite()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data, [])
        self.assertEqual(len(mail.outbox), 0)

    def test_reviewer_already_invited_as_user_is_skipped(self):
        # The invitation went to another address the user's account matched
        factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_email="old-address@example.com",
            invited_user=self.reviewer.user,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

        response = self.invite()

        self.assertEqual(response.data, [])
        self.assertEqual(len(mail.outbox), 0)


class PublicInvitationDetailsTest(test.APITestCase):
    def test_details_include_invitation_date(self):
        pool_member = factories.CallReviewerPoolFactory(
            reviewer=None, invited_email="reviewer@example.com"
        )

        response = self.client.get(
            f"/api/reviewer-invitations/{pool_member.invitation_token}/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["invited_at"], pool_member.invited_at)


class AcceptEmailInvitationWhenAlreadyInPoolTest(test.APITestCase):
    """The person accepting an email invitation may already be in the pool."""

    def setUp(self):
        self.reviewer = factories.ReviewerProfileFactory(is_published=True)
        self.call = factories.CallFactory()
        factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.email_invitation = factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=None,
            invited_email=self.reviewer.user.email,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.client.force_authenticate(self.reviewer.user)

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.email_invitation.refresh_from_db()
        self.assertIsNone(self.email_invitation.reviewer)
        self.assertEqual(
            self.email_invitation.invitation_status,
            ReviewerPoolInvitationStatuses.PENDING,
        )

    def test_accept_by_token(self):
        response = self.client.post(
            f"/api/reviewer-invitations/{self.email_invitation.invitation_token}/accept/"
        )
        self.assert_rejected(response)

    def test_accept_from_pool(self):
        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(
                self.email_invitation, action="accept"
            )
        )
        self.assert_rejected(response)


class AcceptReviewerInvitationByTokenTest(test.APITestCase):
    """A token for a reviewer-bound invitation needs that reviewer's login."""

    def setUp(self):
        self.invitation = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )
        self.url = (
            f"/api/reviewer-invitations/{self.invitation.invitation_token}/accept/"
        )

    def assert_status(self, expected_status):
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.invitation_status, expected_status)

    def test_anonymous_cannot_accept(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assert_status(ReviewerPoolInvitationStatuses.PENDING)

    def test_another_user_cannot_accept(self):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assert_status(ReviewerPoolInvitationStatuses.PENDING)

    def test_reviewer_can_accept(self):
        self.client.force_authenticate(self.invitation.reviewer.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assert_status(ReviewerPoolInvitationStatuses.ACCEPTED)
