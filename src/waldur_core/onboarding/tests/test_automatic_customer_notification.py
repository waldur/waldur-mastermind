from unittest import mock

from django.core import mail
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Notification
from waldur_core.onboarding import enums, tasks
from waldur_core.structure.tests import factories as structure_factories

from . import factories

NOTIFICATION_KEY = "onboarding.customer_created_automatically"


def recipients_of(messages):
    return sorted(address for message in messages for address in message.to)


@override_config(ONBOARDING_NOTIFICATION_EMAILS="onboarding@example.com")
class AutomaticCustomerNotificationTest(APITestCase):
    def setUp(self):
        Notification.objects.update_or_create(
            key=NOTIFICATION_KEY, defaults={"enabled": True}
        )
        self.user = structure_factories.UserFactory(
            email="applicant@example.com", full_name="Alice Applicant"
        )
        self.verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.VERIFIED,
            country="EE",
            legal_person_identifier="12345678",
            legal_name="Secret Company OÜ",
            validation_method=enums.ValidationMethod.ARIREGISTER,
        )
        self.url = factories.OnboardingVerificationFactory.get_url(
            self.verification, action="create_customer"
        )

    def create_customer(self):
        self.client.force_authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response

    @override_settings(task_always_eager=True)
    def test_configured_addresses_are_told_with_an_organization_link(self):
        response = self.create_customer()

        self.assertEqual(recipients_of(mail.outbox), ["onboarding@example.com"])
        message = mail.outbox[0]
        self.assertIn(f"organizations/{response.data['uuid']}/", message.body)
        self.assertIn("Estonian Business Register", message.body)

    @override_settings(task_always_eager=True)
    def test_message_carries_no_personal_or_organization_data(self):
        self.create_customer()

        message = mail.outbox[0]
        html = message.alternatives[0][0]
        for content in (message.subject, message.body, html):
            for secret in (
                "Alice Applicant",
                "applicant@example.com",
                "Secret Company",
                "12345678",
            ):
                self.assertNotIn(secret, content)

    @override_config(ONBOARDING_NOTIFICATION_EMAILS="")
    @mock.patch.object(tasks.send_customer_created_automatically_notification, "delay")
    def test_nothing_is_queued_without_recipients(self, delay):
        self.create_customer()

        delay.assert_not_called()

    @override_settings(task_always_eager=True)
    def test_disabled_notification_sends_nothing(self):
        Notification.objects.filter(key=NOTIFICATION_KEY).update(enabled=False)

        self.create_customer()

        self.assertEqual(len(mail.outbox), 0)

    @mock.patch.object(tasks.send_customer_created_automatically_notification, "delay")
    def test_organization_created_by_staff_is_not_mailed(self, delay):
        # Administration -> Onboarding -> "Create customer": staff saw it.
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        delay.assert_not_called()

    @mock.patch.object(
        tasks.send_customer_created_automatically_notification,
        "delay",
        side_effect=OSError("broker unreachable"),
    )
    def test_queueing_error_does_not_fail_the_creation(self, _delay):
        with self.assertLogs("waldur_core.onboarding.views", "ERROR"):
            self.create_customer()

        self.verification.refresh_from_db()
        self.assertIsNotNone(self.verification.customer)

    def test_disabled_notification_is_reported_as_a_warning(self):
        Notification.objects.filter(key=NOTIFICATION_KEY).update(enabled=False)
        verification = factories.OnboardingVerificationFactory(
            status=enums.VerificationStatus.VERIFIED,
            validation_method=enums.ValidationMethod.ARIREGISTER,
            customer=structure_factories.CustomerFactory(),
        )

        with self.assertLogs("waldur_core.onboarding.tasks", "WARNING") as logs:
            tasks.send_customer_created_automatically_notification(
                verification.uuid.hex
            )

        self.assertEqual(len(mail.outbox), 0)
        self.assertIn(NOTIFICATION_KEY, logs.output[0])

    @override_settings(task_always_eager=True)
    def test_failed_creation_sends_nothing(self):
        self.verification.status = enums.VerificationStatus.FAILED
        self.verification.save()

        self.client.force_authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(len(mail.outbox), 0)


@override_config(ONBOARDING_NOTIFICATION_EMAILS="onboarding@example.com")
class SendCustomerCreatedAutomaticallyNotificationTaskTest(APITestCase):
    def setUp(self):
        Notification.objects.update_or_create(
            key=NOTIFICATION_KEY, defaults={"enabled": True}
        )
        self.verification = factories.OnboardingVerificationFactory(
            status=enums.VerificationStatus.VERIFIED,
            validation_method=enums.ValidationMethod.ARIREGISTER,
            customer=structure_factories.CustomerFactory(),
        )

    def send(self):
        tasks.send_customer_created_automatically_notification(
            self.verification.uuid.hex
        )

    def test_automatically_verified_customer_is_notified(self):
        self.send()

        self.assertEqual(recipients_of(mail.outbox), ["onboarding@example.com"])

    def test_manually_approved_customer_is_skipped(self):
        # Reviewers approved it themselves, so they already know.
        self.verification.validation_method = ""
        self.verification.save()
        factories.OnboardingJustificationFactory(
            verification=self.verification,
            validation_decision=enums.ReviewDecision.APPROVED,
        )

        self.send()

        self.assertEqual(len(mail.outbox), 0)

    def test_register_verified_customer_reviewed_by_staff_is_skipped(self):
        # Escalated after the register confirmed it (e.g. identity review):
        # staff saw it through its justification.
        factories.OnboardingJustificationFactory(
            verification=self.verification,
            validation_decision=enums.ReviewDecision.APPROVED,
        )

        self.send()

        self.assertEqual(len(mail.outbox), 0)

    def test_verification_without_customer_is_skipped(self):
        self.verification.customer = None
        self.verification.save()

        self.send()

        self.assertEqual(len(mail.outbox), 0)

    def test_deleted_verification_is_skipped(self):
        uuid = self.verification.uuid.hex
        self.verification.delete()

        tasks.send_customer_created_automatically_notification(uuid)

        self.assertEqual(len(mail.outbox), 0)

    def test_addresses_are_read_when_sending(self):
        with override_config(ONBOARDING_NOTIFICATION_EMAILS=""):
            self.send()

        self.assertEqual(len(mail.outbox), 0)
