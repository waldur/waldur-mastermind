from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from waldur_core.onboarding import enums, tasks
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
