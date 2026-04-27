from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories

AI_ASSISTANT_ENABLED_CONFIG = {
    "AI_ASSISTANT_ENABLED": True,
    "AI_ASSISTANT_API_URL": "https://example.com/stream",
    "AI_ASSISTANT_API_TOKEN": "dummy-token",
}


class AccessLevelBaseTest(test.APITestCase):
    def setUp(self):
        self.staff_user = structure_factories.UserFactory(is_staff=True)
        self.support_user = structure_factories.UserFactory(is_support=True)
        self.regular_user = structure_factories.UserFactory()

        self.stream_url = reverse("chat-stream")
        self.current_session_url = reverse("chat-session-current")
        self.threads_url = reverse("chat-thread-list")
        self.messages_url = reverse("chat-message-list")
        self.tool_execute_url = reverse("chat-tools-execute-tool")


class StreamAccessLevelTest(AccessLevelBaseTest):
    """Test access level enforcement on the stream endpoint."""

    # --- staff level ---

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_allows_staff(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_denies_support(self):
        self.client.force_authenticate(user=self.support_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_denies_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # --- staff_and_support level ---

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff_and_support"
    )
    def test_staff_and_support_level_allows_staff(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff_and_support"
    )
    def test_staff_and_support_level_allows_support(self):
        self.client.force_authenticate(user=self.support_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff_and_support"
    )
    def test_staff_and_support_level_denies_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # --- all level ---

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="all"
    )
    def test_all_level_allows_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # --- disabled roles ---

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="disabled"
    )
    def test_disabled_roles_denies_staff(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # --- AI_ASSISTANT_ENABLED=False takes precedence ---

    @override_constance_config(
        AI_ASSISTANT_ENABLED=False, AI_ASSISTANT_ENABLED_ROLES="all"
    )
    def test_chat_disabled_returns_424(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertEqual(response.status_code, status.HTTP_424_FAILED_DEPENDENCY)

    # --- unauthenticated ---

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="all"
    )
    def test_unauthenticated_returns_401(self):
        response = self.client.post(self.stream_url, data={"input": "Hello"})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class SessionAccessLevelTest(AccessLevelBaseTest):
    """Test access level enforcement on chat session endpoint."""

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_allows_staff(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(self.current_session_url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_denies_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(self.current_session_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff_and_support"
    )
    def test_staff_and_support_level_allows_support(self):
        self.client.force_authenticate(user=self.support_user)
        response = self.client.get(self.current_session_url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="all"
    )
    def test_all_level_allows_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(self.current_session_url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ThreadAccessLevelTest(AccessLevelBaseTest):
    """Test access level enforcement on thread endpoint."""

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_allows_staff(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(self.threads_url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_denies_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(self.threads_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="all"
    )
    def test_all_level_allows_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(self.threads_url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class MessageAccessLevelTest(AccessLevelBaseTest):
    """Test access level enforcement on message endpoint."""

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_allows_staff(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get(self.messages_url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_denies_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(self.messages_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="all"
    )
    def test_all_level_allows_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.get(self.messages_url)
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ToolAccessLevelTest(AccessLevelBaseTest):
    """Test access level enforcement on tool execute endpoint."""

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_allows_staff(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.post(
            self.tool_execute_url,
            data={"tool": "nonexistent", "arguments": {}},
            format="json",
        )
        # Staff passes access check; 400/422 from tool validation is expected
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="staff"
    )
    def test_staff_level_denies_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(
            self.tool_execute_url,
            data={"tool": "nonexistent", "arguments": {}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="all"
    )
    def test_all_level_allows_regular_user(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(
            self.tool_execute_url,
            data={"tool": "nonexistent", "arguments": {}},
            format="json",
        )
        self.assertNotEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_constance_config(
        **AI_ASSISTANT_ENABLED_CONFIG, AI_ASSISTANT_ENABLED_ROLES="all"
    )
    def test_regular_user_cannot_invoke_staff_only_tool(self):
        """
        Regression: tool execute endpoint must intersect the requested tool
        with the caller's permitted tool set. ``get_user_overview`` is in
        STAFF_TOOLS / SUPPORT_TOOLS only — never in END_USER_TOOLS — so a
        regular user must not be able to invoke it directly even when access
        to the chat feature itself is open to "all".
        """
        victim = structure_factories.UserFactory(email="victim@example.com")

        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(
            self.tool_execute_url,
            data={
                "tool": "get_user_overview",
                "arguments": {"user_email": victim.email},
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # And critically: no victim data must leak in the response body.
        body = response.json() if response.content else {}
        self.assertNotIn("data", body)
