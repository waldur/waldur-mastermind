from smtplib import SMTPRecipientsRefused
from unittest import mock

from django.core import mail
from rest_framework import test

from waldur_core.configuration.forms import SettingsForm
from waldur_core.configuration.testing import override_config
from waldur_core.core import utils
from waldur_core.core.models import Notification, VersionUpdateNotification
from waldur_core.core.tasks import send_version_update_notification
from waldur_core.structure.tests.factories import UserFactory

CURRENT = "8.1.3"
INDEX = {
    "latest_stable": "8.1.4",
    "latest_rc": "8.1.5-rc.2",
    "releases": [],
}
NOTIFICATION_KEY = "core.version_update_available"
SETTING = "VERSION_UPDATE_NOTIFICATION_EMAILS"


def recipients_of(messages):
    return sorted(address for message in messages for address in message.to)


@override_config(VERSION_UPDATE_NOTIFICATION_EMAILS="ops@example.com")
@mock.patch("waldur_core.core.tasks.__version__", CURRENT)
@mock.patch("waldur_core.core.tasks.fetch_changelog_index", return_value=INDEX)
class SendVersionUpdateNotificationTest(test.APITestCase):
    def setUp(self):
        Notification.objects.update_or_create(
            key=NOTIFICATION_KEY, defaults={"enabled": True}
        )

    def test_sends_email_with_version_and_upgrade_links(self, _fetch):
        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["ops@example.com"])
        self.assertIn("8.1.4", message.subject)
        self.assertIn("Waldur 8.1.4 is now available.", message.body)
        self.assertIn(CURRENT, message.body)
        self.assertIn(
            "https://docs.waldur.com/latest/admin-guide/deployment/helm/",
            message.body,
        )
        self.assertIn(
            "https://docs.waldur.com/latest/admin-guide/deployment/docker-compose/",
            message.body,
        )
        html = message.alternatives[0][0]
        self.assertIn("Waldur 8.1.4 is now available.", html)

    def test_records_each_notified_recipient(self, _fetch):
        send_version_update_notification()

        self.assertEqual(
            list(VersionUpdateNotification.objects.values_list("version", "recipient")),
            [("8.1.4", "ops@example.com")],
        )

    @override_config(
        VERSION_UPDATE_NOTIFICATION_EMAILS="ops@example.com, cto@example.com"
    )
    def test_each_recipient_gets_its_own_message(self, _fetch):
        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            recipients_of(mail.outbox), ["cto@example.com", "ops@example.com"]
        )

    @override_config(
        VERSION_UPDATE_NOTIFICATION_EMAILS="ops@example.com, OPS@example.com ,,"
    )
    def test_duplicates_and_blanks_are_ignored(self, _fetch):
        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.com"])

    def test_same_version_is_notified_only_once(self, _fetch):
        send_version_update_notification()
        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 1)

    def test_added_recipient_receives_current_release_alone(self, _fetch):
        send_version_update_notification()

        with override_config(
            VERSION_UPDATE_NOTIFICATION_EMAILS="ops@example.com, new@example.com"
        ):
            send_version_update_notification()

        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[1].to, ["new@example.com"])

    def test_recipient_is_matched_case_insensitively(self, _fetch):
        send_version_update_notification()

        with override_config(VERSION_UPDATE_NOTIFICATION_EMAILS="Ops@Example.com"):
            send_version_update_notification()

        self.assertEqual(len(mail.outbox), 1)

    @override_config(
        VERSION_UPDATE_NOTIFICATION_EMAILS="ops@example.com, bad@example.com"
    )
    def test_failed_recipient_is_not_recorded_and_retried(self, _fetch):
        real_send_mail = utils.send_mail

        def send_mail(subject, body, to, **kwargs):
            if to == ["bad@example.com"]:
                raise SMTPRecipientsRefused({"bad@example.com": (451, b"Try later")})
            return real_send_mail(subject, body, to, **kwargs)

        with mock.patch.object(utils, "send_mail", side_effect=send_mail):
            send_version_update_notification()

        self.assertEqual(recipients_of(mail.outbox), ["ops@example.com"])
        self.assertEqual(
            list(VersionUpdateNotification.objects.values_list("recipient", flat=True)),
            ["ops@example.com"],
        )

        # The next daily run retries only the failed address.
        send_version_update_notification()

        self.assertEqual(
            recipients_of(mail.outbox), ["bad@example.com", "ops@example.com"]
        )

    def test_newer_version_is_notified_again(self, fetch):
        send_version_update_notification()
        fetch.return_value = {**INDEX, "latest_stable": "8.1.5"}

        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("8.1.5", mail.outbox[1].subject)

    @override_config(CHECK_FOR_UPDATES=False)
    def test_github_check_off_does_not_block_email(self, _fetch):
        # CHECK_FOR_UPDATES only gates the GitHub fallback, which this task skips.
        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 1)

    @override_config(VERSION_UPDATE_NOTIFICATION_EMAILS="")
    def test_no_recipients_sends_nothing(self, fetch):
        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 0)
        fetch.assert_not_called()

    def test_up_to_date_sends_nothing(self, fetch):
        fetch.return_value = {**INDEX, "latest_stable": CURRENT}

        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(VersionUpdateNotification.objects.exists())

    def test_ahead_of_latest_sends_nothing(self, fetch):
        # A development build of the next release is ahead of latest_stable.
        fetch.return_value = {**INDEX, "latest_stable": "8.1.2"}

        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 0)

    def test_unavailable_index_sends_nothing(self, fetch):
        fetch.return_value = None

        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 0)

    def test_stable_deployment_is_not_offered_rc(self, _fetch):
        send_version_update_notification()

        self.assertNotIn("rc", mail.outbox[0].subject)

    def test_rc_deployment_is_offered_newer_rc(self, _fetch):
        # A context manager, not a decorator: a method-level patch decorator
        # joins the class-level ones and is applied first, so the class's
        # CURRENT would override it.
        with mock.patch("waldur_core.core.tasks.__version__", "8.1.5-rc.1"):
            send_version_update_notification()

        self.assertIn("8.1.5-rc.2", mail.outbox[0].subject)

    def test_disabled_notification_template_is_respected(self, _fetch):
        Notification.objects.filter(key=NOTIFICATION_KEY).update(enabled=False)

        send_version_update_notification()

        self.assertEqual(len(mail.outbox), 0)
        # Not recorded, so it goes out once the operator re-enables it.
        self.assertFalse(VersionUpdateNotification.objects.exists())


class NotificationEmailsSettingApiTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/override-settings/"
        self.client.force_login(UserFactory(is_staff=True))

    def test_valid_addresses_are_stored_as_typed(self):
        value = "ops@example.com, cto@example.com,"
        response = self.client.post(self.url, {SETTING: value}, format="json")
        self.assertEqual(response.status_code, 200)

        response = self.client.get(self.url)
        self.assertEqual(response.data[SETTING], value)

    def test_malformed_address_is_rejected(self):
        response = self.client.post(
            self.url, {SETTING: "ops@example.com, not-an-email"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("not-an-email", str(response.data[SETTING]))


class NotificationEmailsSettingAdminFormTest(test.APITestCase):
    def submit(self, value):
        form = SettingsForm(data={SETTING: value})
        form.is_valid()
        return form

    def test_valid_addresses_are_accepted(self):
        form = self.submit("ops@example.com, cto@example.com,")

        self.assertNotIn(SETTING, form.errors)

    def test_malformed_address_is_rejected(self):
        form = self.submit("ops@example.com, not-an-email")

        self.assertIn("not-an-email", str(form.errors[SETTING]))
