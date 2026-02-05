"""Tests for onboarding verification deletion event logging."""

from unittest import mock

from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.logging.enums import EventType
from waldur_core.onboarding import enums
from waldur_core.onboarding.models import OnboardingVerification
from waldur_core.structure.tests import factories as structure_factories

from . import factories


class VerificationDeletionLoggingTest(APITestCase):
    """Test that verification deletions are properly logged."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.PENDING,
        )

    def test_manual_deletion_by_authenticated_user_is_logged(self):
        """Test that manual deletion by authenticated user is logged with user info."""
        self.client.force_authenticate(user=self.user)
        url = factories.OnboardingVerificationFactory.get_url(self.verification)

        with mock.patch("waldur_core.logging.event_logger.emit") as logger_mock:
            response = self.client.delete(url)
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

            # Verify event logging was called
            logger_mock.assert_called_once()
            call_args = logger_mock.call_args

            # Check event type
            self.assertEqual(
                call_args[1]["event_type"],
                EventType.ONBOARDING_VERIFICATION_DELETED,
            )

            # Check event context contains verification and user info
            event_context = call_args[1]["event_context"]
            # Compare by UUID since the instance may be stale after deletion
            self.assertEqual(event_context["verification"].uuid, self.verification.uuid)
            self.assertEqual(event_context["deleted_by_username"], self.user.username)
            self.assertEqual(event_context["deleted_by_full_name"], self.user.full_name)
            self.assertEqual(event_context["deleted_by_uuid"], self.user.uuid.hex)

            # Check message contains user info
            message = call_args[0][0]
            self.assertIn("deleted by user", message)

    def test_manual_deletion_by_staff_is_logged(self):
        """Test that manual deletion by staff is logged with staff user info."""
        self.client.force_authenticate(user=self.staff)
        url = factories.OnboardingVerificationFactory.get_url(self.verification)

        with mock.patch("waldur_core.logging.event_logger.emit") as logger_mock:
            response = self.client.delete(url)
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

            # Verify event logging was called
            logger_mock.assert_called_once()
            call_args = logger_mock.call_args

            # Check event type
            self.assertEqual(
                call_args[1]["event_type"],
                EventType.ONBOARDING_VERIFICATION_DELETED,
            )

            # Check event context contains staff user info
            event_context = call_args[1]["event_context"]
            self.assertEqual(event_context["deleted_by_username"], self.staff.username)
            self.assertEqual(
                event_context["deleted_by_full_name"], self.staff.full_name
            )

    def test_task_deletion_is_logged_with_task_event_type(self):
        """Test that automated task deletion is logged with task-specific event type."""
        from datetime import timedelta

        from django.utils import timezone as real_timezone

        # Create an old expired verification
        verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.EXPIRED,
        )
        # Make it old enough for deletion task (31 days ago)
        thirty_one_days_ago = real_timezone.now() - timedelta(days=31)
        OnboardingVerification.objects.filter(id=verification.id).update(
            modified=thirty_one_days_ago
        )

        with mock.patch("waldur_core.logging.event_logger.emit") as logger_mock:
            # Manually mark as task deletion
            verification._deleted_by_task = True
            verification._deleted_by = None
            verification.delete()

            # Verify event logging was called
            logger_mock.assert_called_once()
            call_args = logger_mock.call_args

            # Check event type is task-specific
            self.assertEqual(
                call_args[1]["event_type"],
                EventType.ONBOARDING_VERIFICATION_DELETED_BY_TASK,
            )

            # Check event context does not contain deleted_by user info
            event_context = call_args[1]["event_context"]
            # Compare by UUID since the instance may be stale after deletion
            self.assertEqual(event_context["verification"].uuid, verification.uuid)
            self.assertNotIn("deleted_by_username", event_context)
            self.assertNotIn("deleted_by_full_name", event_context)
            self.assertNotIn("deleted_by_uuid", event_context)

            # Check message mentions scheduled task
            message = call_args[0][0]
            self.assertIn("scheduled task", message)

    def test_verification_is_deleted_from_database(self):
        """Test that verification is actually removed from database after deletion."""
        self.client.force_authenticate(user=self.user)
        url = factories.OnboardingVerificationFactory.get_url(self.verification)
        verification_uuid = self.verification.uuid

        with mock.patch("waldur_core.logging.event_logger.emit"):
            response = self.client.delete(url)
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify verification is deleted
        self.assertFalse(
            OnboardingVerification.objects.filter(uuid=verification_uuid).exists()
        )

    def test_deletion_without_deleted_by_metadata_is_logged(self):
        """Test that deletion without _deleted_by metadata is still logged."""
        with mock.patch("waldur_core.logging.event_logger.emit") as logger_mock:
            # Delete without setting _deleted_by
            self.verification.delete()

            # Verify event logging was called
            logger_mock.assert_called_once()
            call_args = logger_mock.call_args

            # Check event type
            self.assertEqual(
                call_args[1]["event_type"],
                EventType.ONBOARDING_VERIFICATION_DELETED,
            )

            # Check event context does not contain deleted_by user info
            event_context = call_args[1]["event_context"]
            self.assertNotIn("deleted_by_username", event_context)
            self.assertNotIn("deleted_by_full_name", event_context)
            self.assertNotIn("deleted_by_uuid", event_context)

            # Check message is generic
            message = call_args[0][0]
            self.assertNotIn("deleted by user", message)
            self.assertNotIn("scheduled task", message)
