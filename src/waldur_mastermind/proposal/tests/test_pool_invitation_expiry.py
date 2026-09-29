"""Reviewer pool invitations expire on every invitation path."""

from datetime import timedelta

from django.core import mail
from django.test import override_settings
from django.utils import timezone
from freezegun import freeze_time
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

NOW = "2026-03-10T12:00:00Z"


class ManagerSetupMixin:
    def setUp(self):
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.call = factories.CallFactory()
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        self.call.manager.add_user(self.manager, CallRole.MANAGER)
        structure_factories.NotificationFactory(key="proposal.reviewer_invitation")
        self.client.force_authenticate(self.manager)

    def configure_expiration(self, days):
        models.CallAssignmentConfiguration.objects.create(
            call=self.call, assignment_expiration_days=days
        )


@freeze_time(NOW)
@override_settings(task_always_eager=True)
class InvitationPathsSetExpiryTest(ManagerSetupMixin, test.APITestCase):
    def invite_registered(self):
        reviewer = factories.ReviewerProfileFactory()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.CallFactory.get_protected_url(
                    self.call, action="reviewer-pool"
                ),
                {"reviewer_uuids": [reviewer.uuid.hex]},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return models.CallReviewerPool.objects.get(call=self.call, reviewer=reviewer)

    def invite_by_email(self):
        response = self.client.post(
            factories.CallFactory.get_protected_url(
                self.call, action="invite-by-email"
            ),
            {"email": "someone@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return models.CallReviewerPool.objects.get(
            call=self.call, invited_email="someone@example.com"
        )

    def invite_from_suggestions(self):
        reviewer = factories.ReviewerProfileFactory()
        models.ReviewerSuggestion.objects.create(
            call=self.call,
            reviewer=reviewer,
            affinity_score=0.8,
            status=ReviewerSuggestionStatuses.CONFIRMED,
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                factories.CallFactory.get_protected_url(
                    self.call, action="send-invitations"
                )
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return models.CallReviewerPool.objects.get(call=self.call, reviewer=reviewer)

    def assert_expires_in(self, entry, days):
        self.assertEqual(entry.invitation_expires_at, timezone.now() + timedelta(days))

    def test_every_path_uses_call_setting(self):
        self.configure_expiration(3)
        for invite in (
            self.invite_registered,
            self.invite_by_email,
            self.invite_from_suggestions,
        ):
            with self.subTest(path=invite.__name__):
                self.assert_expires_in(invite(), 3)

    def test_every_path_defaults_to_seven_days_without_configuration(self):
        for invite in (
            self.invite_registered,
            self.invite_by_email,
            self.invite_from_suggestions,
        ):
            with self.subTest(path=invite.__name__):
                self.assert_expires_in(invite(), 7)


@override_settings(task_always_eager=True)
class MarkExpiredInvitationsTaskTest(test.APITestCase):
    def setUp(self):
        structure_factories.NotificationFactory(
            key="proposal.reviewer_pool_invitation_expired"
        )
        self.inviter = structure_factories.UserFactory()
        self.entry = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            invited_by=self.inviter,
            invitation_expires_at=timezone.now() - timedelta(minutes=1),
        )

    def test_pending_past_date_becomes_expired_and_inviter_notified_once(self):
        tasks.mark_expired_reviewer_pool_invitations()
        tasks.mark_expired_reviewer_pool_invitations()

        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.EXPIRED
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.inviter.email])
        self.assertIn(self.entry.call.name, mail.outbox[0].subject)
        self.assertIn(self.entry.reviewer.user.full_name, mail.outbox[0].body)

    def test_future_undated_and_answered_invitations_untouched(self):
        future = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            invitation_expires_at=timezone.now() + timedelta(days=1),
        )
        undated = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            invitation_expires_at=None,
        )
        accepted = factories.CallReviewerPoolFactory(
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
            invitation_expires_at=timezone.now() - timedelta(days=1),
        )

        tasks.mark_expired_reviewer_pool_invitations()

        for entry, expected in (
            (future, ReviewerPoolInvitationStatuses.PENDING),
            (undated, ReviewerPoolInvitationStatuses.PENDING),
            (accepted, ReviewerPoolInvitationStatuses.ACCEPTED),
        ):
            entry.refresh_from_db()
            self.assertEqual(entry.invitation_status, expected)

    def test_call_managers_notified_when_inviter_is_gone(self):
        self.entry.invited_by = None
        self.entry.save()
        call_manager = structure_factories.UserFactory()
        self.entry.call.add_user(call_manager, CallRole.MANAGER)

        tasks.mark_expired_reviewer_pool_invitations()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [call_manager.email])

    def test_email_invitation_names_address(self):
        self.entry.reviewer = None
        self.entry.invited_email = "invitee@example.com"
        self.entry.save()

        tasks.mark_expired_reviewer_pool_invitations()

        self.assertIn("invitee@example.com", mail.outbox[0].body)


class ExpiredInvitationResponseTest(test.APITestCase):
    def setUp(self):
        self.reviewer_user = structure_factories.UserFactory()
        self.profile = factories.ReviewerProfileFactory(
            user=self.reviewer_user, is_published=True
        )
        self.entry = factories.CallReviewerPoolFactory(
            reviewer=self.profile,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
            invitation_expires_at=timezone.now() - timedelta(days=1),
        )

    def post(self, url, data):
        self.client.force_authenticate(self.reviewer_user)
        return self.client.post(url, data, format="json")

    def pool_url(self, action):
        return factories.CallReviewerPoolFactory.get_url(self.entry, action=action)

    def token_url(self, action):
        return f"http://testserver/api/reviewer-invitations/{self.entry.invitation_token}/{action}/"

    def assert_refused(self, response):
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("expired", str(response.data))
        self.entry.refresh_from_db()
        self.assertNotIn(
            self.entry.invitation_status,
            (
                ReviewerPoolInvitationStatuses.ACCEPTED,
                ReviewerPoolInvitationStatuses.DECLINED,
            ),
        )

    def test_past_date_refused_on_every_response_path(self):
        for url, data in (
            (self.pool_url("accept"), []),
            (self.pool_url("decline"), {"reason": "busy"}),
            (self.token_url("accept"), {}),
            (self.token_url("decline"), {"reason": "busy"}),
        ):
            with self.subTest(url=url):
                self.assert_refused(self.post(url, data))

    def test_expired_status_refused_on_every_response_path(self):
        self.entry.invitation_status = ReviewerPoolInvitationStatuses.EXPIRED
        self.entry.invitation_expires_at = None
        self.entry.save()
        for url, data in (
            (self.pool_url("accept"), []),
            (self.pool_url("decline"), {"reason": "busy"}),
            (self.token_url("accept"), {}),
            (self.token_url("decline"), {"reason": "busy"}),
        ):
            with self.subTest(url=url):
                self.assert_refused(self.post(url, data))

    def test_public_details_report_expired_status(self):
        self.entry.invitation_status = ReviewerPoolInvitationStatuses.EXPIRED
        self.entry.invitation_expires_at = None
        self.entry.save()
        response = self.client.get(
            f"http://testserver/api/reviewer-invitations/{self.entry.invitation_token}/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_expired"])


@freeze_time(NOW)
@override_settings(task_always_eager=True)
class ResendInvitationTest(ManagerSetupMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.entry = factories.CallReviewerPoolFactory(
            call=self.call,
            invitation_status=ReviewerPoolInvitationStatuses.EXPIRED,
            invitation_expires_at=timezone.now() - timedelta(days=2),
        )

    def resend(self, entry=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                factories.CallReviewerPoolFactory.get_url(
                    entry or self.entry, action="resend-invitation"
                )
            )

    def test_expired_invitation_resent_with_new_expiry(self):
        self.configure_expiration(5)

        response = self.resend()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_status, ReviewerPoolInvitationStatuses.PENDING
        )
        self.assertEqual(
            self.entry.invitation_expires_at, timezone.now() + timedelta(days=5)
        )
        self.assertEqual(response.data["invitation_status"], "pending")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.entry.reviewer.user.email])

    def test_resending_replaces_the_token_link(self):
        old_token = self.entry.invitation_token

        self.resend()

        self.entry.refresh_from_db()
        self.assertNotEqual(self.entry.invitation_token, old_token)
        # The email carries the new link only.
        self.assertIn(self.entry.invitation_token, mail.outbox[0].body)
        self.assertNotIn(old_token, mail.outbox[0].body)
        # A copy of the old link, however it got around, no longer works.
        self.client.force_authenticate(None)
        response = self.client.get(
            f"http://testserver/api/reviewer-invitations/{old_token}/"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.get(
            f"http://testserver/api/reviewer-invitations/{self.entry.invitation_token}/"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_resent_invitation_can_be_accepted(self):
        self.resend()
        self.client.force_authenticate(self.entry.reviewer.user)
        self.entry.reviewer.is_published = True
        self.entry.reviewer.save()

        response = self.client.post(
            factories.CallReviewerPoolFactory.get_url(self.entry, action="accept"),
            [],
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_pending_invitation_can_be_resent(self):
        self.entry.invitation_status = ReviewerPoolInvitationStatuses.PENDING
        self.entry.invitation_expires_at = None
        self.entry.save()

        response = self.resend()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.entry.refresh_from_db()
        self.assertEqual(
            self.entry.invitation_expires_at, timezone.now() + timedelta(days=7)
        )

    def test_answered_invitation_cannot_be_resent(self):
        for answered in (
            ReviewerPoolInvitationStatuses.ACCEPTED,
            ReviewerPoolInvitationStatuses.DECLINED,
        ):
            with self.subTest(status=answered):
                self.entry.invitation_status = answered
                self.entry.save()
                response = self.resend()
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(len(mail.outbox), 0)

    def test_reviewer_cannot_resend_own_invitation(self):
        self.client.force_authenticate(self.entry.reviewer.user)

        response = self.resend()

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertEqual(len(mail.outbox), 0)
