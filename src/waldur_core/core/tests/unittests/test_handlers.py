from unittest import mock

from django.test import TestCase, override_settings

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories

NOTIFICATION_SETTINGS = {
    "NOTIFICATIONS_PROFILE_CHANGES": {
        "ENABLE_OPERATOR_OWNER_NOTIFICATIONS": True,
        "OPERATOR_NOTIFICATION_EMAILS": ["test@example.com"],
    }
}


@mock.patch("waldur_core.core.log.event_logger.info")
class LogUserSaveTest(TestCase):
    """Tests for logging and notifications when user data is updated."""

    def setUp(self):
        self.user = factories.UserFactory(full_name="John", email="john@example.org")

    def test_sent_notification_if_change_owner_email(
        self, mock_event_logger: mock.Mock
    ):
        """
        When organization owner changes their email:
        - Event should be logged once
        """
        customer = factories.CustomerFactory()
        customer.add_user(self.user, CustomerRole.OWNER)
        mock_event_logger.reset_mock()
        old_email = self.user.email
        new_email = "new_email_" + old_email
        self.user.email = new_email
        self.user.save()

        self.assertEqual(
            mock_event_logger.call_count,
            1,
            "Expected one event to be logged when owner's email is changed",
        )

    @mock.patch("waldur_core.core.utils.broadcast_mail")
    @override_settings(WALDUR_CORE=NOTIFICATION_SETTINGS)
    def test_notification_message_and_email_context(
        self, mock_broadcast_mail: mock.Mock, mock_event_logger: mock.Mock
    ):
        """
        When organization owner changes their email:
        - Email notification should be sent once
        - Event should be logged with correct message
        - Email context should contain old and new values
        """
        customer = factories.CustomerFactory(name="Customer", abbreviation="ABC")
        customer.add_user(self.user, CustomerRole.OWNER)
        old_email = self.user.email
        new_email = "new_email_" + old_email
        self.user.email = new_email
        self.user.save()

        # Verify email notification was sent
        self.assertEqual(
            mock_broadcast_mail.call_count,
            1,
            "Expected one email notification to be sent",
        )

        # Verify correct email template was used
        template = mock_broadcast_mail.call_args[0][0:2]
        self.assertEqual(
            template,
            ("structure", "notifications_profile_changes_operator"),
            "Incorrect email template was used",
        )

        # Verify event log message
        msg = mock_event_logger.call_args[0][0]
        context = mock_event_logger.call_args[1]["event_context"]
        test_msg = msg.format(affected_user_username=context["affected_user"].username)
        expected_msg = (
            f"User {self.user.username} has been updated. Details:\n"
            f"email: {old_email} -> {new_email}"
        )
        self.assertEqual(
            test_msg, expected_msg, f"Expected message: {expected_msg}, Got: {test_msg}"
        )

        # Verify email context
        context = mock_broadcast_mail.call_args[0][2]
        expected_field = {
            "name": "email",
            "old_value": old_email,
            "new_value": new_email,
        }
        self.assertEqual(
            context["fields"],
            [expected_field],
            f"Expected email context fields: {[expected_field]}, Got: {context['fields']}",
        )

    def test_dont_sent_notification_if_change_owner_other_field(
        self, mock_event_logger: mock.Mock
    ):
        """
        When organization owner changes a non-whitelisted field (token_lifetime):
        - No event should be logged because token_lifetime is not in User.WHITELIST_FIELDS
        """
        customer = factories.CustomerFactory()
        customer.add_user(self.user, CustomerRole.OWNER)
        mock_event_logger.reset_mock()
        token_lifetime = 100 + self.user.token_lifetime
        self.user.token_lifetime = token_lifetime
        self.user.save()

        self.assertListEqual(
            [
                call
                for call in mock_event_logger.mock_calls
                if call[2].get("event_type") == "user_update_succeeded"
            ],
            [],
            "Expected no events to be logged when owner changes non-whitelisted field",
        )

    @mock.patch("waldur_core.core.utils.broadcast_mail")
    @override_settings(WALDUR_CORE=NOTIFICATION_SETTINGS)
    def test_dont_sent_notification_if_change_not_owner_email(
        self, mock_broadcast_mail: mock.Mock, mock_event_logger: mock.Mock
    ):
        """
        When non-owner changes their email:
        - Event should be logged
        - No email notification should be sent
        """
        new_email = "new_email_" + self.user.email
        self.user.email = new_email
        self.user.save()

        # Verify event is logged
        self.assertEqual(
            mock_event_logger.call_count,
            1,
            "Expected one event to be logged when non-owner changes email",
        )
        self.assertEqual(
            mock_event_logger.call_args[1]["event_type"],
            "user_update_succeeded",
            "Incorrect event type logged",
        )

        # Verify no email notification
        self.assertEqual(
            mock_broadcast_mail.call_count,
            0,
            "Expected no email notifications for non-owner email change",
        )
