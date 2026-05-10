from django.core.exceptions import ValidationError
from rest_framework import test

from waldur_core.core import validators
from waldur_core.media.validators import validate_notification_emails
from waldur_core.structure.models import Customer


class NameValidationTest(test.APITestCase):
    def test_name_should_have_at_least_one_non_whitespace_character(self):
        with self.assertRaises(ValidationError):
            customer = Customer(name="      ")
            customer.full_clean()


class CIDRListValidatorTest(test.APITestCase):
    def test_validator_accepts_valid_cidr_list(self):
        validators.validate_cidr_list("fc00::/7, 127.0.0.1/32")

    def test_validator_accepts_empty_list(self):
        validators.validate_cidr_list("   ")

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            validators.validate_cidr_list("hello/25")

    def test_space_separated_list_rejected(self):
        with self.assertRaises(ValidationError):
            validators.validate_cidr_list("fc00::/7  127.0.0.1/32")

    def test_bare_ip_without_prefix_is_rejected(self):
        with self.assertRaises(ValidationError):
            validators.validate_cidr_list("192.168.1.1")
        with self.assertRaises(ValidationError):
            validators.validate_cidr_list("2001:db8::1")

    def test_host_bits_are_accepted(self):
        validators.validate_cidr_list("192.168.1.5/24, 2001:db8::1/64")


class NotificationEmailsValidatorTest(test.APITestCase):
    def test_validator_accepts_valid_emails(self):
        validate_notification_emails("user@localhost")

        validate_notification_emails("user1@localhost,user2@localhost")

        validate_notification_emails(" user1@localhost , user2@localhost ")

        validate_notification_emails("")

        validate_notification_emails(None)

    def test_validator_rejects_invalid_emails(self):
        with self.assertRaises(ValidationError) as cm:
            validate_notification_emails("invalid-email")
        self.assertIn("Invalid email address: invalid-email", str(cm.exception))

        with self.assertRaises(ValidationError) as cm:
            validate_notification_emails("user1@localhost,invalid-email")
        self.assertIn("Invalid email address: invalid-email", str(cm.exception))
