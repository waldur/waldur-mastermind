import logging

from constance import config
from rest_framework.exceptions import PermissionDenied

from waldur_mastermind.chat.injection_detection import SeverityLevel
from waldur_mastermind.chat.models import Message
from waldur_mastermind.chat.prompts import (
    REJECTION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TOOL_INSTRUCTIONS,
)
from waldur_mastermind.chat.tools import get_tools_prompt

logger = logging.getLogger(__name__)

# Messages at or above MEDIUM severity are excluded from LLM context history.
# This threshold is derived from SeverityLevel.MEDIUM's score range lower bound.
INJECTION_HISTORY_EXCLUSION_THRESHOLD = SeverityLevel.MEDIUM.get_score_range()[0]


def _get_thread_messages(thread):
    """Return active, non-injection messages queryset for a thread, respecting history limit.

    Excludes messages with injection score >= 0.5 (MEDIUM or higher severity).
    LOW severity messages are kept since they only indicate possible, not confirmed, injections.

    Returns None if history limit is invalid, empty queryset if no messages.
    """
    limit = config.LLM_CHAT_HISTORY_LIMIT
    if not isinstance(limit, int) or limit <= 0:
        logger.warning(
            "Invalid LLM_CHAT_HISTORY_LIMIT value: %s.",
            limit,
        )
        return None

    all_messages = Message.objects.filter(thread=thread, replaced_by__isnull=True)
    filtered = all_messages.exclude(
        injection_score__gte=INJECTION_HISTORY_EXCLUSION_THRESHOLD
    ).order_by("sequence_index")[:limit]

    if logger.isEnabledFor(logging.DEBUG):
        excluded_count = all_messages.filter(
            injection_score__gte=INJECTION_HISTORY_EXCLUSION_THRESHOLD
        ).count()
        if excluded_count:
            logger.debug(
                "Excluded %d flagged message(s) from history for thread %s",
                excluded_count,
                thread.uuid,
            )

    return filtered


def build_context(user, user_input, thread=None):
    """
    Build the complete prompt string for the LLM.

    Assembles:
      1. System prompt (persona + tool definitions + UI capabilities)
      2. Conversation history from DB (limited by LLM_CHAT_HISTORY_LIMIT, chronological)
      3. Current user message
    """
    if thread and thread.chat_session.user != user:
        raise PermissionDenied("Thread does not belong to the requesting user.")

    tools_prompt = get_tools_prompt()
    tool_instructions = TOOL_INSTRUCTIONS.format(tools=tools_prompt)
    system_prompt = SYSTEM_PROMPT.format(tools=tool_instructions)

    parts = [system_prompt]

    if thread:
        history = _get_conversation_history(thread)
        if history:
            parts.append(f"=== CONVERSATION HISTORY ===\n{history}")

    parts.append(f"=== CURRENT USER MESSAGE ===\nuser: {user_input}")

    return "\n\n".join(parts)


def _get_conversation_history(thread):
    messages = _get_thread_messages(thread)
    if messages is None or not messages.exists():
        return ""
    lines = [
        f"{role}: {content}"
        for role, content in messages.values_list("role", "content")
    ]
    return "\n".join(lines)


def build_rejection_input(thread):
    """Build LLM input for context-aware rejection using recent conversation history.

    Returns None if no thread or no history (caller should fall back to static message).
    """
    if not thread:
        return None

    history = _get_conversation_history(thread)
    if not history:
        return None

    return f"{REJECTION_SYSTEM_PROMPT}\n\nConversation history:\n{history}"
