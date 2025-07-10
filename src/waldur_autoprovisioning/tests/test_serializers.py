from django.test import TestCase

from waldur_autoprovisioning.serializers import RuleSerializer
from waldur_core.structure.tests import factories as structure_factories


class RuleSerializerTest(TestCase):
    def setUp(self):
        self.valid_data = {
            "customer": structure_factories.CustomerFactory.get_url(),
            "user_email_patterns": [".*@example.com", "test@.*"],
            "user_affiliations": ["staff"],
        }

    def test_valid_regex_patterns_accepted(self):
        serializer = RuleSerializer(data=self.valid_data)
        self.assertTrue(serializer.is_valid())

    def test_invalid_regex_patterns_rejected(self):
        invalid_data = self.valid_data.copy()
        invalid_data["user_email_patterns"] = [
            "*invalid",
            ".+@example.com",
            "+alsoinvalid",
        ]

        serializer = RuleSerializer(data=invalid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("user_email_patterns", serializer.errors)
        self.assertIn(
            "Invalid regex patterns", str(serializer.errors["user_email_patterns"])
        )

    def test_empty_patterns_accepted(self):
        empty_data = self.valid_data.copy()
        empty_data["user_email_patterns"] = []

        serializer = RuleSerializer(data=empty_data)
        self.assertTrue(serializer.is_valid())

    def test_none_patterns_accepted(self):
        none_data = self.valid_data.copy()
        del none_data["user_email_patterns"]

        serializer = RuleSerializer(data=none_data)
        self.assertTrue(serializer.is_valid())

    def test_mixed_valid_invalid_patterns_rejected(self):
        mixed_data = self.valid_data.copy()
        mixed_data["user_email_patterns"] = [".*@example.com", "*invalid", "test@.*"]

        serializer = RuleSerializer(data=mixed_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("user_email_patterns", serializer.errors)
        self.assertIn("*invalid", str(serializer.errors["user_email_patterns"]))

    def test_non_string_patterns_rejected(self):
        invalid_data = self.valid_data.copy()
        invalid_data["user_email_patterns"] = [".*@example.com", 123, None]

        serializer = RuleSerializer(data=invalid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("user_email_patterns", serializer.errors)
