import json
from unittest import mock

from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.input_guards.service import _reset_for_testing
from waldur_mastermind.chat.models import Message
from waldur_mastermind.chat.tests.utils import (
    _make_content_chunk,
    _mock_openai_client,
    text_from_blocks,
)


class PIIIntegrationBaseTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)
        self.stream_url = reverse("chat-stream")
        _reset_for_testing()

    def tearDown(self):
        _reset_for_testing()


class CredentialBlockTest(PIIIntegrationBaseTest):
    """Test that credentials are blocked at the API level."""

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_INJECTION_ALLOWLIST="",
    )
    def test_private_key_blocked(self):
        """Private key in message should be blocked and PII fields persisted."""
        response = self.client.post(
            self.stream_url,
            data={
                "input": "Here is my key: -----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK..."
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = b"".join(response.streaming_content).decode()
        # Should get canned rejection (blocked input)
        self.assertIn("I can't help with that request", content)

        # Verify PII detection fields are persisted
        user_msg = Message.objects.filter(
            thread__chat_session__user=self.user, role="user"
        ).first()
        self.assertIsNotNone(user_msg)
        self.assertTrue(user_msg.is_flagged)
        self.assertEqual(user_msg.action_taken, "block")
        self.assertIn("pii_private_key", user_msg.pii_categories)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_INJECTION_ALLOWLIST="",
    )
    def test_github_token_blocked(self):
        response = self.client.post(
            self.stream_url,
            data={"input": "Use this token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = b"".join(response.streaming_content).decode()
        self.assertIn("I can't help with that request", content)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_INJECTION_ALLOWLIST="",
    )
    def test_credential_block_yields_pii_warning(self):
        """Blocked credential message should include a PII warning in the stream."""
        response = self.client.post(
            self.stream_url,
            data={"input": "Use this token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = b"".join(response.streaming_content).decode()

        # Should contain a warning NDJSON event with credential-specific message
        found_warning = False
        for line in content.strip().split("\n"):
            try:
                obj = json.loads(line)
                if "w" in obj:
                    found_warning = True
                    self.assertIn("blocked", obj["w"].lower())
                    self.assertIn("credentials", obj["w"].lower())
            except json.JSONDecodeError:
                continue
        self.assertTrue(found_warning, "Expected PII warning for blocked credentials")


class IBANRedactTest(PIIIntegrationBaseTest):
    """Test that IBANs are redacted before reaching the AI Assistant."""

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_INJECTION_ALLOWLIST="",
    )
    def test_iban_redacted_in_llm_context(self, mock_openai_cls):
        """IBAN should be redacted before sending to AI Assistant."""
        mock_client = _mock_openai_client(
            [_make_content_chunk("I can help with that.")]
        )
        mock_openai_cls.return_value = mock_client

        response = self.client.post(
            self.stream_url,
            data={"input": "My IBAN is EE382200221020145685 please transfer"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = b"".join(response.streaming_content).decode()

        # Verify AI Assistant was called (not blocked) — REDACT should still call AI Assistant
        self.assertTrue(
            mock_openai_cls.called,
            f"Expected AI Assistant to be called for REDACT action. Stream content: {content[:200]}",
        )

        # Verify the IBAN was redacted in the messages sent to AI Assistant
        call_kwargs = mock_client.chat.completions.create.call_args
        sent_messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get(
            "messages"
        )
        all_contents = " ".join(m["content"] for m in sent_messages)
        self.assertNotIn("EE382200221020145685", all_contents)
        self.assertIn("REDACTED", all_contents)

        # Verify PII detection fields are persisted
        user_msg = Message.objects.filter(
            thread__chat_session__user=self.user, role="user"
        ).first()
        self.assertIsNotNone(user_msg)
        self.assertTrue(user_msg.is_flagged)
        self.assertEqual(user_msg.action_taken, "redact")
        # Stored content should contain redacted text, not original IBAN
        stored_text = text_from_blocks(user_msg.blocks)
        self.assertNotIn("EE382200221020145685", stored_text)
        self.assertIn("REDACTED", stored_text)

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_INJECTION_ALLOWLIST="",
    )
    def test_iban_redact_yields_pii_warning(self, mock_openai_cls):
        """Redacted message should include a PII warning in the stream."""
        mock_client = _mock_openai_client([_make_content_chunk("I can help.")])
        mock_openai_cls.return_value = mock_client

        response = self.client.post(
            self.stream_url,
            data={"input": "My IBAN is EE382200221020145685 for payment"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = b"".join(response.streaming_content).decode()

        # Should contain a warning NDJSON event
        found_warning = False
        for line in content.strip().split("\n"):
            try:
                obj = json.loads(line)
                if "w" in obj:
                    found_warning = True
                    self.assertIn("redacted", obj["w"].lower())
            except json.JSONDecodeError:
                continue
        self.assertTrue(found_warning, "Expected PII warning in stream")


class JWTWarnTest(PIIIntegrationBaseTest):
    """Test that JWTs produce a warning but don't block."""

    @mock.patch("waldur_mastermind.chat.llm_streamer.openai.OpenAI")
    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_INJECTION_ALLOWLIST="",
    )
    def test_jwt_warned_but_not_blocked(self, mock_openai_cls):
        mock_client = _mock_openai_client(
            [_make_content_chunk("I see you have a token.")]
        )
        mock_openai_cls.return_value = mock_client

        jwt_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        response = self.client.post(
            self.stream_url,
            data={"input": f"Parse this JWT: {jwt_token}"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = b"".join(response.streaming_content).decode()

        # Should NOT be blocked (AI Assistant was called)
        self.assertTrue(mock_openai_cls.called)
        # Should contain a warning
        found_warning = False
        for line in content.strip().split("\n"):
            try:
                obj = json.loads(line)
                if "w" in obj:
                    found_warning = True
            except json.JSONDecodeError:
                continue
        self.assertTrue(found_warning, "Expected PII warning for JWT")

        # Verify PII detection fields are persisted
        user_msg = Message.objects.filter(
            thread__chat_session__user=self.user, role="user"
        ).first()
        self.assertIsNotNone(user_msg)
        self.assertTrue(user_msg.is_flagged)
        self.assertEqual(user_msg.action_taken, "warn")
        self.assertIn("pii_jwt", user_msg.pii_categories)


class DatabaseURLBlockTest(PIIIntegrationBaseTest):
    """Test that database connection strings are blocked at the API level."""

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_INJECTION_ALLOWLIST="",
    )
    def test_database_url_blocked(self):
        """Database connection string should be blocked."""
        response = self.client.post(
            self.stream_url,
            data={
                "input": "Connect using postgres://admin:secretpassword@db.example.com:5432/mydb"
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = b"".join(response.streaming_content).decode()
        self.assertIn("I can't help with that request", content)

        user_msg = Message.objects.filter(
            thread__chat_session__user=self.user, role="user"
        ).first()
        self.assertIsNotNone(user_msg)
        self.assertTrue(user_msg.is_flagged)
        self.assertEqual(user_msg.action_taken, "block")
        self.assertIn("pii_database_url", user_msg.pii_categories)

    @override_constance_config(
        AI_ASSISTANT_ENABLED=True,
        AI_ASSISTANT_ENABLED_ROLES="all",
        AI_ASSISTANT_API_URL="https://example.com/stream",
        AI_ASSISTANT_API_TOKEN="dummy-token",
        AI_ASSISTANT_INJECTION_ALLOWLIST="",
    )
    def test_password_context_blocked(self):
        """Password=value context should be blocked."""
        response = self.client.post(
            self.stream_url,
            data={"input": "Login with password=MySuperSecretPass123!"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = b"".join(response.streaming_content).decode()
        self.assertIn("I can't help with that request", content)

        user_msg = Message.objects.filter(
            thread__chat_session__user=self.user, role="user"
        ).first()
        self.assertIsNotNone(user_msg)
        self.assertTrue(user_msg.is_flagged)
        self.assertEqual(user_msg.action_taken, "block")
