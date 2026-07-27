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


class NormalizeNetworkAclTest(test.APITestCase):
    def test_bare_ipv4_is_widened_to_host_route(self):
        self.assertEqual(
            validators.normalize_network_acl(["203.0.113.5"]), ["203.0.113.5/32"]
        )

    def test_bare_ipv6_is_widened_to_host_route(self):
        self.assertEqual(
            validators.normalize_network_acl(["2001:db8::1"]), ["2001:db8::1/128"]
        )

    def test_ipv6_is_case_canonicalised(self):
        self.assertEqual(
            validators.normalize_network_acl(["2001:DB8::1"]), ["2001:db8::1/128"]
        )

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(
            validators.normalize_network_acl([" 203.0.113.0/24 "]), ["203.0.113.0/24"]
        )

    def test_networks_are_preserved(self):
        self.assertEqual(
            validators.normalize_network_acl(["203.0.113.0/24", "2001:db8::/32"]),
            ["203.0.113.0/24", "2001:db8::/32"],
        )

    def test_duplicates_are_collapsed_preserving_order(self):
        self.assertEqual(
            validators.normalize_network_acl(
                ["203.0.113.5", "203.0.113.5/32", "198.51.100.0/24"]
            ),
            ["203.0.113.5/32", "198.51.100.0/24"],
        )

    def test_host_bits_set_is_rejected_with_suggestion(self):
        with self.assertRaises(ValidationError) as ctx:
            validators.normalize_network_acl(["203.0.113.5/24"])
        self.assertIn("203.0.113.0/24", str(ctx.exception))

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValidationError):
            validators.normalize_network_acl(["not-an-ip"])

    def test_default_route_is_rejected(self):
        for entry in ("0.0.0.0/0", "::/0"):
            with self.assertRaises(ValidationError):
                validators.normalize_network_acl([entry])

    def test_non_list_is_rejected(self):
        with self.assertRaises(ValidationError):
            validators.normalize_network_acl("203.0.113.0/24")

    def test_empty_list_is_allowed(self):
        self.assertEqual(validators.normalize_network_acl([]), [])


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
