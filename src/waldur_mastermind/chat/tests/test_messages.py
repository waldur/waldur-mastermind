from constance.test.unittest import override_config as override_constance_config
from django.urls import reverse
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.models import ChatSession, Message, ThreadSession
from waldur_mastermind.chat.tests.utils import blocks_from_text, markdown_block


@override_constance_config(
    AI_ASSISTANT_ENABLED=True,
    AI_ASSISTANT_ENABLED_ROLES="all",
    AI_ASSISTANT_API_URL="https://example.com/stream",
    AI_ASSISTANT_API_TOKEN="dummy-token",
)
class MessageViewSetTest(test.APITestCase):
    """Test MessageViewSet endpoints."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()
        self.client.force_authenticate(user=self.user)

        self.session = ChatSession.objects.create(user=self.user)
        self.thread = ThreadSession.objects.create(chat_session=self.session)

        self.other_session = ChatSession.objects.create(user=self.other_user)
        self.other_thread = ThreadSession.objects.create(
            chat_session=self.other_session
        )

        self.list_url = reverse("chat-message-list")

    def test_list_returns_only_user_messages(self):
        """list endpoint returns only current user's messages."""
        my_msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("My message"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.other_thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Other message"),
            sequence_index=1,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(my_msg.uuid))

    def test_list_excludes_replaced_messages_by_default(self):
        """list endpoint excludes replaced messages by default."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Original"),
            sequence_index=1,
        )
        replacement = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Edited"),
            sequence_index=1,
            replaces=original,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(replacement.uuid))

    def test_list_includes_replaced_when_include_history(self):
        """list endpoint includes replaced messages when include_history=true."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Original"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Edited"),
            sequence_index=1,
            replaces=original,
        )

        response = self.client.get(self.list_url, {"include_history": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_include_history_false_excludes_replaced(self):
        """include_history=false correctly excludes replaced messages (not truthy string)."""
        original = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Original"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("Edited"),
            sequence_index=1,
            replaces=original,
        )

        response = self.client.get(self.list_url, {"include_history": "false"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["blocks"][0]["content"], "Edited")

    def test_blocks_text_only(self):
        """Message with a single markdown block is serialized as-is."""
        msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Hello world"),
            sequence_index=1,
        )
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = next(d for d in response.data if d["uuid"] == str(msg.uuid))
        self.assertEqual(len(data["blocks"]), 1)
        self.assertEqual(data["blocks"][0]["key"], "markdown")
        self.assertEqual(data["blocks"][0]["content"], "Hello world")

    def test_blocks_tool_only(self):
        """Message with a tool block is serialized with tool + nested result."""
        tool_block = {
            "id": "blk_0",
            "key": "tool",
            "status": "complete",
            "tool": {
                "call_id": "call_x",
                "name": "list_projects",
                "arguments": {},
                "summary": "",
            },
            "result": markdown_block("", blk_id="blk_0_r"),
        }
        msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=[tool_block],
            sequence_index=1,
        )
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = next(d for d in response.data if d["uuid"] == str(msg.uuid))
        self.assertEqual(len(data["blocks"]), 1)
        self.assertEqual(data["blocks"][0]["key"], "tool")
        self.assertEqual(data["blocks"][0]["tool"]["name"], "list_projects")

    def test_blocks_text_and_tool(self):
        """Message carrying both a text block and a tool block round-trips."""
        text_block = markdown_block("Let me look that up.")
        tool_block = {
            "id": "blk_1",
            "key": "tool",
            "status": "complete",
            "tool": {
                "call_id": "call_y",
                "name": "show_user_resources",
                "arguments": {"limit": "5"},
                "summary": "",
            },
            "result": markdown_block("", blk_id="blk_1_r"),
        }
        msg = Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=[text_block, tool_block],
            sequence_index=1,
        )
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = next(d for d in response.data if d["uuid"] == str(msg.uuid))
        self.assertEqual(len(data["blocks"]), 2)
        self.assertEqual(data["blocks"][0]["content"], "Let me look that up.")
        self.assertEqual(data["blocks"][1]["tool"]["arguments"], {"limit": "5"})

    def test_filter_by_thread(self):
        """list endpoint can filter messages by thread UUID."""
        thread2 = ThreadSession.objects.create(chat_session=self.session)

        msg1 = Message.objects.create(
            thread=self.thread,
            role=Message.Role.USER,
            blocks=blocks_from_text("T1"),
            sequence_index=1,
        )
        Message.objects.create(
            thread=thread2,
            role=Message.Role.USER,
            blocks=blocks_from_text("T2"),
            sequence_index=1,
        )

        response = self.client.get(self.list_url, {"thread": str(self.thread.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(msg1.uuid))

    def test_message_token_fields_visible_to_regular_users(self):
        """Regular users should see input_tokens/output_tokens on messages."""
        Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Reply"),
            sequence_index=1,
            input_tokens=100,
            output_tokens=40,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data[0]
        self.assertEqual(data["input_tokens"], 100)
        self.assertEqual(data["output_tokens"], 40)

    def test_message_token_fields_visible_to_staff(self):
        """Staff users should see input_tokens/output_tokens on messages."""
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        Message.objects.create(
            thread=self.thread,
            role=Message.Role.ASSISTANT,
            blocks=blocks_from_text("Reply"),
            sequence_index=1,
            input_tokens=100,
            output_tokens=40,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data[0]
        self.assertEqual(data["input_tokens"], 100)
        self.assertEqual(data["output_tokens"], 40)
