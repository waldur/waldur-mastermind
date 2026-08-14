from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from rest_framework import test

from waldur_core.core.validators import (
    is_potentially_dangerous_regex,
    matches_access_email_pattern,
    validate_access_email_patterns,
)
from waldur_core.structure.tests.factories import UserFactory


class MatchesAccessEmailPatternTest(SimpleTestCase):
    def test_pattern_must_match_the_whole_email(self):
        """A prefix match would admit lookalike domains."""
        self.assertTrue(
            matches_access_email_pattern([r".*@example\.com"], "user@example.com")
        )
        self.assertFalse(
            matches_access_email_pattern(
                [r".*@example\.com"], "user@example.com.attacker.net"
            )
        )

    def test_matching_is_case_insensitive(self):
        self.assertTrue(
            matches_access_email_pattern([r".*@example\.com"], "User@EXAMPLE.CoM")
        )

    def test_any_pattern_may_match(self):
        patterns = [r".*@example\.com", r".*@example\.org"]
        self.assertTrue(matches_access_email_pattern(patterns, "user@example.org"))
        self.assertFalse(matches_access_email_pattern(patterns, "user@example.net"))

    def test_unusable_patterns_never_match(self):
        """A broken configuration must deny access, not grant it."""
        for pattern in ("*broken", r"(a+)+@example\.com", "", None, 42):
            with self.subTest(pattern=pattern):
                self.assertFalse(
                    matches_access_email_pattern([pattern], "aaa@example.com")
                )

    def test_a_broken_pattern_does_not_hide_a_valid_one(self):
        self.assertTrue(
            matches_access_email_pattern(
                ["*broken", r".*@example\.com"], "user@example.com"
            )
        )

    def test_missing_email_never_matches(self):
        for email in ("", None, 42):
            with self.subTest(email=email):
                self.assertFalse(matches_access_email_pattern([r".*"], email))

    def test_empty_or_invalid_pattern_list_never_matches(self):
        self.assertFalse(matches_access_email_pattern([], "user@example.com"))
        self.assertFalse(
            matches_access_email_pattern(r".*@example\.com", "user@example.com")
        )


class RedosDetectionTest(SimpleTestCase):
    """The shared guard behind every regex the platform accepts from an admin."""

    def test_nested_quantifiers_are_detected(self):
        for pattern in (
            r"(a+)+@example\.com",
            r"(?P<x>a+)+@example\.com",
            r"(?:a+)*@example\.com",
            r"(a{1,3})+@example\.com",
            r"(\w+)+@example\.com",
            "a+?+",
            "a" * 201,
        ):
            with self.subTest(pattern=pattern[:30]):
                self.assertTrue(is_potentially_dangerous_regex(pattern))

    def test_ordinary_patterns_are_not_flagged(self):
        for pattern in (
            r".*@example\.com",
            r".*@(example|test)\.com",
            r"admin@.*",
            r"[a-z]+@example\.com",
            r"(sub\.)*example\.com",
            r"^user[0-9]{2,4}@example\.com$",
        ):
            with self.subTest(pattern=pattern):
                self.assertFalse(is_potentially_dangerous_regex(pattern))


class ValidateAccessEmailPatternsTest(SimpleTestCase):
    def test_valid_patterns_are_accepted(self):
        validate_access_email_patterns([r".*@example\.com", r"admin@.*"])

    def test_invalid_regex_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_access_email_patterns(["*broken"])

    def test_dangerous_regex_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_access_email_patterns([r"(a+)+@example\.com"])

    def test_non_list_value_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_access_email_patterns(r".*@example\.com")


class AllowedEmailPatternsSettingTest(test.APITestCase):
    """The setting is validated on write so a typo cannot silently lock users out."""

    def setUp(self):
        self.url = "/api/override-settings/"
        self.client.force_login(UserFactory(is_staff=True))

    def test_valid_patterns_are_stored(self):
        response = self.client.post(
            self.url,
            {"OIDC_ALLOWED_USER_EMAIL_PATTERNS": [r".*@example\.com"]},
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get(self.url)
        self.assertEqual(
            response.data["OIDC_ALLOWED_USER_EMAIL_PATTERNS"], [r".*@example\.com"]
        )

    def test_invalid_pattern_is_rejected(self):
        response = self.client.post(
            self.url, {"OIDC_ALLOWED_USER_EMAIL_PATTERNS": ["*broken"]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("OIDC_ALLOWED_USER_EMAIL_PATTERNS", response.data)
