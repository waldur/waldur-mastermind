from smtplib import SMTPRecipientsRefused
from unittest import mock

from django.core import mail
from django.test import TestCase

from waldur_core.core import utils
from waldur_core.core.models import Notification
from waldur_core.logging.models import EmailLog


@mock.patch("waldur_core.core.utils.render_to_string", return_value="html")
@mock.patch("waldur_core.core.utils.format_text", return_value="text")
@mock.patch("waldur_core.core.utils.find_template_from_registry", return_value="path")
@mock.patch("waldur_core.core.utils.send_mail")
class BroadcastMailTest(TestCase):
    def setUp(self):
        Notification.objects.create(key="app.event", enabled=True)
        self.recipients = ["first@example.com", "bad@example.com", "last@example.com"]

    def test_failed_recipient_does_not_block_remaining_recipients(
        self, mock_send_mail, *mocks
    ):
        def side_effect(subject, body, to, **kwargs):
            if to == ["bad@example.com"]:
                raise SMTPRecipientsRefused({"bad@example.com": (550, b"No such user")})
            return 1

        mock_send_mail.side_effect = side_effect

        with self.assertLogs("waldur_core.core.utils", level="ERROR") as logs:
            utils.broadcast_mail("app", "event", {}, self.recipients)

        sent_to = [call.kwargs["to"] for call in mock_send_mail.call_args_list]
        self.assertEqual(
            sent_to,
            [["first@example.com"], ["bad@example.com"], ["last@example.com"]],
        )
        self.assertTrue(any("bad@example.com" in line for line in logs.output))

    def test_recipients_share_one_connection(self, mock_send_mail, *mocks):
        utils.broadcast_mail("app", "event", {}, self.recipients)

        connections = {
            call.kwargs["connection"] for call in mock_send_mail.call_args_list
        }
        self.assertEqual(len(connections), 1)
        self.assertIsNotNone(connections.pop())


@mock.patch("waldur_core.core.utils.render_to_string", return_value="html")
@mock.patch("waldur_core.core.utils.format_text", return_value="text")
@mock.patch("waldur_core.core.utils.find_template_from_registry", return_value="path")
@mock.patch("waldur_core.core.utils.send_mail")
class DroppedNotificationIsLoggedTest(TestCase):
    """A dropped mail used to leave no trace at all.

    Notifications ship disabled, so "no mail arrived" is the symptom of a
    switched-off notification, an unregistered one, and a genuine routing bug
    alike. Without a log line there is nothing for an operator to go on.
    """

    recipients = ["first@example.com", "second@example.com"]

    def test_disabled_notification_is_logged(self, mock_send_mail, *mocks):
        Notification.objects.create(key="app.event", enabled=False)

        with self.assertLogs("waldur_core.core.utils", level="INFO") as logs:
            utils.broadcast_mail("app", "event", {}, self.recipients)

        mock_send_mail.assert_not_called()
        self.assertTrue(any("app.event" in line for line in logs.output))
        self.assertTrue(any("disabled" in line for line in logs.output))

    def test_unregistered_notification_is_logged(self, mock_send_mail, *mocks):
        with self.assertLogs("waldur_core.core.utils", level="WARNING") as logs:
            utils.broadcast_mail("app", "event", {}, self.recipients)

        mock_send_mail.assert_not_called()
        self.assertTrue(any("app.event" in line for line in logs.output))
        self.assertTrue(any("not registered" in line for line in logs.output))


@mock.patch("waldur_core.core.utils.render_to_string", return_value="html")
@mock.patch("waldur_core.core.utils.format_text", return_value="text")
@mock.patch("waldur_core.core.utils.find_template_from_registry", return_value="path")
class BroadcastMailSingleMessageTest(TestCase):
    """single_message turns the broadcast into one message everyone can see."""

    def setUp(self):
        Notification.objects.create(key="app.event", enabled=True)
        mail.outbox = []

    def test_by_default_every_recipient_gets_a_private_copy(self, *mocks):
        utils.broadcast_mail("app", "event", {}, ["a@example.com", "b@example.com"])

        self.assertEqual(
            [message.to for message in mail.outbox],
            [["a@example.com"], ["b@example.com"]],
        )

    def test_by_default_non_string_recipients_still_work(self, *mocks):
        # Some callers pass objects rather than address strings; the default
        # path hands them on without inspecting them.
        recipient = type("User", (), {"__str__": lambda self: "u@example.com"})()
        with mock.patch.object(utils, "send_mail") as send_mail:
            utils.broadcast_mail("app", "event", {}, [recipient, recipient])
        self.assertEqual(send_mail.call_count, 2)

    def test_single_message_addresses_everyone_in_to(self, *mocks):
        utils.broadcast_mail(
            "app",
            "event",
            {},
            ["a@example.com", "b@example.com"],
            single_message=True,
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["a@example.com", "b@example.com"])
        self.assertEqual(mail.outbox[0].cc, [])

    def test_single_message_lists_each_mailbox_once(self, *mocks):
        utils.broadcast_mail(
            "app",
            "event",
            {},
            ["a@example.com", "A@Example.com", "b@example.com", ""],
            single_message=True,
        )

        self.assertEqual(mail.outbox[0].to, ["a@example.com", "b@example.com"])

    def test_single_message_is_logged_with_every_recipient(self, *mocks):
        utils.broadcast_mail(
            "app",
            "event",
            {},
            ["a@example.com", "b@example.com"],
            single_message=True,
        )

        log = EmailLog.objects.get()
        self.assertEqual(log.emails, ["a@example.com", "b@example.com"])

    def test_single_message_with_non_string_recipients(self, *mocks):
        # Recipients that are not address strings are compared by their
        # string form, as the mail backend renders them.
        recipient = type("User", (), {"__str__": lambda self: "u@example.com"})()
        with mock.patch.object(utils, "send_mail") as send_mail:
            utils.broadcast_mail(
                "app",
                "event",
                {},
                [recipient, "U@example.com", "c@example.com"],
                single_message=True,
            )
        send_mail.assert_called_once()
        self.assertEqual(send_mail.call_args.kwargs["to"], [recipient, "c@example.com"])

    def test_single_message_without_recipients_sends_nothing(self, *mocks):
        utils.broadcast_mail("app", "event", {}, [], single_message=True)

        self.assertEqual(mail.outbox, [])
