from smtplib import SMTPRecipientsRefused
from unittest import mock

from django.test import TestCase

from waldur_core.core import utils
from waldur_core.core.models import Notification


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
