from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from waldur_core.onboarding import enums, tasks
from waldur_core.onboarding.models import OnboardingVerification
from waldur_core.structure.tests import factories as structure_factories

from . import factories


class ExpireStaleVerificationsTest(TestCase):
    def test_task_expires_only_stale_verifications(self):
        """
        Test that when the task runs, it expires only verifications that:
        1. Have expires_at in the past
        2. Have status PENDING or ESCALATED
        """
        user = structure_factories.UserFactory()
        now = timezone.now()

        # These SHOULD be expired by the task (expires_at in the past)
        pending_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.PENDING,
            expires_at=now - timedelta(hours=1),
            legal_person_identifier="11111111",
        )

        escalated_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.ESCALATED,
            expires_at=now - timedelta(hours=2),
            legal_person_identifier="22222222",
        )

        already_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.EXPIRED,
            expires_at=now - timedelta(hours=3),
            legal_person_identifier="33333333",
        )

        # These should NOT be expired by the task (expires_at in the future or status FAILED)
        pending_not_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.PENDING,
            expires_at=now + timedelta(hours=1),
            legal_person_identifier="44444444",
        )

        escalated_not_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.ESCALATED,
            expires_at=now + timedelta(hours=2),
            legal_person_identifier="55555555",
        )

        failed_not_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.FAILED,
            expires_at=now + timedelta(hours=1),
            legal_person_identifier="66666666",
        )
        failed_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.FAILED,
            expires_at=now - timedelta(hours=2),
            legal_person_identifier="77777777",
        )

        tasks.expire_stale_verifications()

        # Verify that 2 stale verifications were expired
        pending_expired.refresh_from_db()
        self.assertEqual(pending_expired.status, enums.VerificationStatus.EXPIRED)
        self.assertEqual(pending_expired.error_message, "VERIFICATION_EXPIRED")
        self.assertIn("expired", pending_expired.error_traceback.lower())

        escalated_expired.refresh_from_db()
        self.assertEqual(escalated_expired.status, enums.VerificationStatus.EXPIRED)
        self.assertEqual(escalated_expired.error_message, "VERIFICATION_EXPIRED")

        # Verify that already expired verification was not touched
        already_expired.refresh_from_db()
        self.assertEqual(already_expired.status, enums.VerificationStatus.EXPIRED)

        # Verify that non-expired verifications were NOT touched
        pending_not_expired.refresh_from_db()
        self.assertEqual(pending_not_expired.status, enums.VerificationStatus.PENDING)
        self.assertEqual(pending_not_expired.error_message, "")

        escalated_not_expired.refresh_from_db()
        self.assertEqual(
            escalated_not_expired.status, enums.VerificationStatus.ESCALATED
        )
        self.assertEqual(escalated_not_expired.error_message, "")

        # Verify that FAILED verifications were NOT touched
        failed_not_expired.refresh_from_db()
        self.assertEqual(failed_not_expired.status, enums.VerificationStatus.FAILED)
        self.assertEqual(failed_not_expired.error_message, "")
        failed_expired.refresh_from_db()
        self.assertEqual(failed_expired.status, enums.VerificationStatus.FAILED)
        self.assertEqual(failed_expired.error_message, "")


class DeleteOldVerificationsTest(TestCase):
    def test_task_deletes_only_old_failed_and_expired_verifications(self):
        """
        Test that when the task runs, it deletes only verifications that:
        1. Have modified timestamp older than 30 days
        2. Have status FAILED or EXPIRED
        """
        user = structure_factories.UserFactory()
        now = timezone.now()
        thirty_one_days_ago = now - timedelta(days=31)

        # These SHOULD be deleted (old and FAILED/EXPIRED)
        old_failed = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.FAILED,
            legal_person_identifier="11111111",
            modified=thirty_one_days_ago,
        )

        old_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.EXPIRED,
            legal_person_identifier="22222222",
            modified=thirty_one_days_ago,
        )

        # These should NOT be deleted (recent or wrong status)
        recent_failed = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.FAILED,
            legal_person_identifier="33333333",
        )

        recent_expired = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.EXPIRED,
            legal_person_identifier="44444444",
        )

        old_pending = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.PENDING,
            legal_person_identifier="55555555",
        )

        old_verified = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.VERIFIED,
            legal_person_identifier="66666666",
        )

        tasks.delete_old_verifications()

        # Verify that old FAILED and EXPIRED verifications were deleted
        self.assertFalse(
            OnboardingVerification.objects.filter(pk=old_failed.pk).exists()
        )
        self.assertFalse(
            OnboardingVerification.objects.filter(pk=old_expired.pk).exists()
        )

        # Verify that recent or non-FAILED/EXPIRED verifications still exist
        self.assertTrue(
            OnboardingVerification.objects.filter(pk=recent_failed.pk).exists()
        )
        self.assertTrue(
            OnboardingVerification.objects.filter(pk=recent_expired.pk).exists()
        )
        self.assertTrue(
            OnboardingVerification.objects.filter(pk=old_pending.pk).exists()
        )
        self.assertTrue(
            OnboardingVerification.objects.filter(pk=old_verified.pk).exists()
        )

    def test_task_handles_empty_queryset(self):
        """Test that the task handles gracefully when no old verifications exist."""
        user = structure_factories.UserFactory()

        # Create only recent verifications
        recent_failed = factories.OnboardingVerificationFactory(
            user=user,
            status=enums.VerificationStatus.FAILED,
            legal_person_identifier="11111111",
        )

        tasks.delete_old_verifications()

        # Verify that the recent verification still exists
        self.assertTrue(
            OnboardingVerification.objects.filter(pk=recent_failed.pk).exists()
        )


class SendJustificationReviewNotificationTest(APITestCase):
    def setUp(self):
        structure_factories.NotificationFactory(
            key="onboarding.justification_review_notification"
        )
        self.user = structure_factories.UserFactory(
            email="test@example.com", full_name="Test User"
        )
        self.verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.PENDING,
            legal_person_identifier="12345678",
            legal_name="Test Company",
        )

        self.justification = factories.OnboardingJustificationFactory(
            verification=self.verification,
            user_justification="Test justification",
        )
        self.approve_url = factories.OnboardingJustificationFactory.get_url(
            justification=self.justification, action="approve"
        )
        self.reject_url = factories.OnboardingJustificationFactory.get_url(
            justification=self.justification, action="reject"
        )
        self.staff = structure_factories.UserFactory(is_staff=True)

    @override_settings(task_always_eager=True)
    def test_send_notification_for_approved_justification(self):
        """Test that notification is sent when justification is approved."""
        self.client.force_authenticate(self.staff)
        self.client.post(self.approve_url)

        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]

        self.assertIn("organization onboarding application", sent_email.subject.lower())
        self.assertEqual(sent_email.to, ["test@example.com"])
        self.assertIn("Test User", sent_email.body)
        self.assertIn("Test Company", sent_email.body)

    @override_settings(task_always_eager=True)
    def test_send_notification_for_rejected_justification(self):
        """Test that notification is sent when justification is rejected."""
        self.client.force_authenticate(self.staff)
        self.client.post(self.reject_url)

        self.assertEqual(len(mail.outbox), 1)
        sent_email = mail.outbox[0]

        self.assertEqual(sent_email.to, ["test@example.com"])
        self.assertIn("Test User", sent_email.body)
        self.assertIn("Test Company", sent_email.body)
