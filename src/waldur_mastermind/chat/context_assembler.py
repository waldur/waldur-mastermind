import logging

from constance import config
from rest_framework.exceptions import PermissionDenied

from waldur_mastermind.chat.models import Message
from waldur_mastermind.chat.prompts import SYSTEM_PROMPT, TOOL_INSTRUCTIONS
from waldur_mastermind.chat.tools import get_tools_prompt

logger = logging.getLogger(__name__)


def build_context(user, user_input, thread=None, include_history=True):
    """
    Build the complete prompt string for the LLM.

    Assembles:
      1. System prompt (persona + tool definitions + UI capabilities)
      2. Conversation history from DB (last 50 messages, chronological)
      3. Current user message
    """
    if thread and thread.chat_session.user != user:
        raise PermissionDenied("Thread does not belong to the requesting user.")

    tools_prompt = get_tools_prompt()
    tool_instructions = TOOL_INSTRUCTIONS.format(tools=tools_prompt)
    system_prompt = SYSTEM_PROMPT.format(tools=tool_instructions)

    parts = [system_prompt]

    if include_history and thread and config.LLM_CHAT_STORAGE_ENABLED:
        # Only include history if we're configured to store it and a thread is provided
        history = _get_conversation_history(thread)
        if history:
            parts.append(f"=== CONVERSATION HISTORY ===\n{history}")

    parts.append(f"=== CURRENT USER MESSAGE ===\nuser: {user_input}")

    return "\n\n".join(parts)


def _get_conversation_history(thread):
    limit = config.LLM_CHAT_HISTORY_LIMIT
    if not isinstance(limit, int) or limit <= 0:
        logger.warning(
            "Invalid LLM_CHAT_HISTORY_LIMIT value: %s. No chat history added as a context.",
            limit,
        )
        return ""
    messages = (
        Message.objects.filter(
            thread=thread,
            replaced_by__isnull=True,
        )
        .order_by("sequence_index")
        .values_list("role", "content")[:limit]
    )

    if not messages:
        return ""

    lines = [f"{role}: {content}" for role, content in messages]
    return "\n".join(lines)
