"""Tests for the public ``/api/configuration/`` endpoint exposure of
anonymous chat settings.

The negative test asserting that secrets do not leak into the
configuration payload is non-negotiable — losing it would silently
broadcast the LLM API token, the user_slug salt, or the
feedback HMAC secret to every anonymous caller.
"""

import json

from constance.test.unittest import override_config as override_constance_config
from django.core.cache import cache
from rest_framework import status, test


class AnonymousConfigurationExposureTest(test.APITestCase):
    def setUp(self):
        self.url = "/api/configuration/"
        cache.delete("API_CONFIGURATION")

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="anonymous",
        AI_ASSISTANT_NAME="HPC Test Assistant",
    )
    def test_anonymous_can_read_chat_flags(self):
        # Anonymous caller — no auth header, no force_login.
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        core_section = response.data.get("WALDUR_CORE") or {}
        self.assertIn("AI_ASSISTANT_ENABLED", core_section)
        self.assertIn("AI_ASSISTANT_ENABLED_ROLES", core_section)
        self.assertIn("AI_ASSISTANT_NAME", core_section)
        self.assertTrue(core_section["AI_ASSISTANT_ENABLED"])
        self.assertEqual(core_section["AI_ASSISTANT_ENABLED_ROLES"], "anonymous")
        self.assertEqual(core_section["AI_ASSISTANT_NAME"], "HPC Test Assistant")


class AnonymousConfigurationSecretsLeakTest(test.APITestCase):
    """Non-negotiable: the public configuration response MUST NOT contain
    any of the anonymous-chat secrets — neither the keys themselves nor
    the literal values. Test against the actual configured values so a
    future allowlist regression (e.g. someone adds ``ANONYMOUS_CHAT_*``
    as a glob) trips a real comparison, not just a key-name check.
    """

    def setUp(self):
        self.url = "/api/configuration/"
        cache.delete("API_CONFIGURATION")

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="anonymous",
        AI_ASSISTANT_API_URL="https://llm.test.example/v1",
        AI_ASSISTANT_API_TOKEN="leaktest-llm-token-abc123",
        ANONYMOUS_CHAT_USER_SLUG_SALT="leaktest-slug-salt-xyz",
        ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="leaktest-feedback-secret-789",
    )
    def test_secrets_never_appear_in_response(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = json.dumps(response.data)

        # Key-name negative checks — defends against an allowlist regression.
        for forbidden_key in (
            "AI_ASSISTANT_API_URL",
            "AI_ASSISTANT_API_TOKEN",
            "ANONYMOUS_CHAT_USER_SLUG_SALT",
            "ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET",
        ):
            self.assertNotIn(forbidden_key, body)

        # Substring checks against the literal Constance values — catches
        # the case where the key name is renamed but the value still
        # appears under a different umbrella section.
        for forbidden_value in (
            "leaktest-llm-token-abc123",
            "leaktest-slug-salt-xyz",
            "leaktest-feedback-secret-789",
            "https://llm.test.example/v1",
        ):
            self.assertNotIn(forbidden_value, body)
