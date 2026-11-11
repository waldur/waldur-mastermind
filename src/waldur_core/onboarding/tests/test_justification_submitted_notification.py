from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.throttling import ScopedRateThrottle

from waldur_core.configuration.forms import SettingsForm
from waldur_core.configuration.testing import override_config
from waldur_core.core.models import Notification
from waldur_core.onboarding import enums, tasks, views
from waldur_core.structure.tests import factories as structure_factories

from . import factories

NOTIFICATION_KEY = "onboarding.justification_submitted"
SETTING = "ONBOARDING_NOTIFICATION_EMAILS"


def recipients_of(messages):
    return sorted(address for message in messages for address in message.to)


@override_config(ONBOARDING_NOTIFICATION_EMAILS="reviewers@example.com")
class JustificationSubmittedNotificationTest(APITestCase):
    def setUp(self):
        Notification.objects.update_or_create(
            key=NOTIFICATION_KEY, defaults={"enabled": True}
        )
        self.user = structure_factories.UserFactory(
            email="applicant@example.com", full_name="Alice Applicant"
        )
        self.verification = factories.OnboardingVerificationFactory(
            user=self.user,
            status=enums.VerificationStatus.ESCALATED,
            country="EE",
            legal_person_identifier="12345678",
            legal_name="Secret Company OÜ",
        )
        self.url = factories.OnboardingJustificationFactory.get_list_url(
            action="create_justification"
        )

    def submit(self, **extra):
        self.client.force_authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url,
                {"verification_uuid": self.verification.uuid.hex, **extra},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response

    @override_settings(task_always_eager=True)
    def test_configured_addresses_are_told_with_a_review_link(self):
        response = self.submit()

        self.assertEqual(recipients_of(mail.outbox), ["reviewers@example.com"])
        message = mail.outbox[0]
        self.assertIn(
            f"support/onboarding/justifications/{response.data['uuid']}/",
            message.body,
        )

    @override_settings(task_always_eager=True)
    def test_message_carries_no_personal_or_organization_data(self):
        self.submit(user_justification="We are a research group at the university")

        message = mail.outbox[0]
        html = message.alternatives[0][0]
        for content in (message.subject, message.body, html):
            for secret in (
                "Alice Applicant",
                "applicant@example.com",
                "Secret Company",
                "12345678",
                "research group",
            ):
                self.assertNotIn(secret, content)

    @override_config(
        ONBOARDING_NOTIFICATION_EMAILS="a@example.com, b@example.com, A@example.com,"
    )
    @override_settings(task_always_eager=True)
    def test_each_distinct_address_gets_its_own_message(self):
        self.submit()

        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(recipients_of(mail.outbox), ["a@example.com", "b@example.com"])

    @override_config(ONBOARDING_NOTIFICATION_EMAILS="")
    @override_settings(task_always_eager=True)
    def test_empty_address_list_sends_nothing(self):
        self.submit()

        self.assertEqual(len(mail.outbox), 0)

    @override_settings(task_always_eager=True)
    def test_disabled_notification_sends_nothing(self):
        Notification.objects.filter(key=NOTIFICATION_KEY).update(enabled=False)

        self.submit()

        self.assertEqual(len(mail.outbox), 0)

    @override_settings(task_always_eager=True)
    def test_resubmitting_for_the_same_verification_mails_once(self):
        # Homeport submits again when a document upload failed.
        self.submit()
        self.submit()

        self.assertEqual(len(mail.outbox), 1)

    @mock.patch.object(
        tasks.send_justification_submitted_notification,
        "apply_async",
        side_effect=OSError("broker unreachable"),
    )
    def test_queueing_error_does_not_fail_the_submission(self, _apply_async):
        with self.assertLogs("waldur_core.onboarding.views", "ERROR"):
            self.submit()

    @mock.patch.object(tasks.send_justification_submitted_notification, "apply_async")
    def test_sending_is_delayed_until_documents_are_attached(self, apply_async):
        response = self.submit()

        apply_async.assert_called_once_with(
            args=[response.data["uuid"]],
            countdown=tasks.JUSTIFICATION_SUBMITTED_NOTIFICATION_DELAY,
        )

    @mock.patch.object(tasks.send_justification_submitted_notification, "apply_async")
    def test_nothing_is_queued_without_recipients(self, apply_async):
        with override_config(ONBOARDING_NOTIFICATION_EMAILS=""):
            self.submit()

        apply_async.assert_not_called()


@override_config(ONBOARDING_NOTIFICATION_EMAILS="reviewers@example.com")
class SendJustificationSubmittedNotificationTaskTest(APITestCase):
    def setUp(self):
        Notification.objects.update_or_create(
            key=NOTIFICATION_KEY, defaults={"enabled": True}
        )
        self.justification = factories.OnboardingJustificationFactory()

    def test_pending_justification_is_notified(self):
        tasks.send_justification_submitted_notification(self.justification.uuid.hex)

        self.assertEqual(recipients_of(mail.outbox), ["reviewers@example.com"])

    def test_justification_deleted_before_sending_is_skipped(self):
        uuid = self.justification.uuid.hex
        self.justification.delete()

        tasks.send_justification_submitted_notification(uuid)

        self.assertEqual(len(mail.outbox), 0)

    def test_justification_reviewed_before_sending_is_skipped(self):
        self.justification.validation_decision = enums.ReviewDecision.APPROVED
        self.justification.save()

        tasks.send_justification_submitted_notification(self.justification.uuid.hex)

        self.assertEqual(len(mail.outbox), 0)

    def test_addresses_are_read_when_sending(self):
        with override_config(ONBOARDING_NOTIFICATION_EMAILS=""):
            tasks.send_justification_submitted_notification(self.justification.uuid.hex)

        self.assertEqual(len(mail.outbox), 0)

    def test_older_pending_justification_already_told_reviewers(self):
        newer = factories.OnboardingJustificationFactory(
            verification=self.justification.verification
        )

        tasks.send_justification_submitted_notification(newer.uuid.hex)

        self.assertEqual(len(mail.outbox), 0)

    def test_older_reviewed_justification_does_not_hold_back_a_new_one(self):
        self.justification.validation_decision = enums.ReviewDecision.REJECTED
        self.justification.save()
        newer = factories.OnboardingJustificationFactory(
            verification=self.justification.verification
        )

        tasks.send_justification_submitted_notification(newer.uuid.hex)

        self.assertEqual(recipients_of(mail.outbox), ["reviewers@example.com"])

    def test_disabled_notification_is_reported_as_a_warning(self):
        Notification.objects.filter(key=NOTIFICATION_KEY).update(enabled=False)

        with self.assertLogs("waldur_core.onboarding.tasks", "WARNING") as logs:
            tasks.send_justification_submitted_notification(self.justification.uuid.hex)

        self.assertEqual(len(mail.outbox), 0)
        self.assertIn(NOTIFICATION_KEY, logs.output[0])


class CreateJustificationThrottleTest(APITestCase):
    def setUp(self):
        cache.clear()
        for patch in (
            mock.patch.object(
                views.OnboardingJustificationViewSet,
                "throttle_classes",
                [ScopedRateThrottle],
            ),
            mock.patch.object(
                ScopedRateThrottle,
                "THROTTLE_RATES",
                {"onboarding_justification": "2/min"},
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(self.user)
        self.url = factories.OnboardingJustificationFactory.get_list_url(
            action="create_justification"
        )

    def create(self):
        verification = factories.OnboardingVerificationFactory(
            user=self.user, status=enums.VerificationStatus.ESCALATED
        )
        return self.client.post(
            self.url, {"verification_uuid": verification.uuid.hex}, format="json"
        )

    def test_creating_justifications_is_throttled_per_user(self):
        statuses = [self.create().status_code for _ in range(3)]

        self.assertEqual(
            statuses,
            [
                status.HTTP_201_CREATED,
                status.HTTP_201_CREATED,
                status.HTTP_429_TOO_MANY_REQUESTS,
            ],
        )

    def test_other_actions_are_not_throttled(self):
        list_url = factories.OnboardingJustificationFactory.get_list_url()
        statuses = {self.client.get(list_url).status_code for _ in range(3)}

        self.assertEqual(statuses, {status.HTTP_200_OK})


class ReviewNotificationEmailsSettingApiTest(APITestCase):
    def setUp(self):
        self.url = "/api/override-settings/"
        self.client.force_login(structure_factories.UserFactory(is_staff=True))

    def test_valid_addresses_are_stored_as_typed(self):
        value = "reviewers@example.com, support@example.com,"
        response = self.client.post(self.url, {SETTING: value}, format="json")
        self.assertEqual(response.status_code, 200)

        response = self.client.get(self.url)
        self.assertEqual(response.data[SETTING], value)

    def test_malformed_address_is_rejected(self):
        response = self.client.post(
            self.url, {SETTING: "reviewers@example.com, not-an-email"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("not-an-email", str(response.data[SETTING]))


class ReviewNotificationEmailsSettingAdminFormTest(APITestCase):
    def submit(self, value):
        form = SettingsForm(data={SETTING: value})
        form.is_valid()
        return form

    def test_valid_addresses_are_accepted(self):
        form = self.submit("reviewers@example.com, support@example.com,")

        self.assertNotIn(SETTING, form.errors)

    def test_malformed_address_is_rejected(self):
        form = self.submit("reviewers@example.com, not-an-email")

        self.assertIn("not-an-email", str(form.errors[SETTING]))
