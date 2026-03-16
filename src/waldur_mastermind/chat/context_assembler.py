import json
import logging

from constance import config
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from waldur_mastermind.chat.input_guards import SeverityLevel
from waldur_mastermind.chat.models import Message
from waldur_mastermind.chat.prompts import (
    REJECTION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)

# Messages with injection at MEDIUM severity or above are excluded from LLM context history.
# LOW severity messages are kept since they only indicate possible, not confirmed, injections.
# PII-only messages (no injection categories) are kept because their content is already redacted.
EXCLUDED_SEVERITIES = [
    SeverityLevel.MEDIUM.value,
    SeverityLevel.HIGH.value,
    SeverityLevel.CRITICAL.value,
]


def _get_thread_messages(thread):
    """Return active, non-blocked messages queryset for a thread, respecting history limit.

    Excludes messages with injection severity >= MEDIUM (medium, high, critical).
    LOW severity messages are kept since they only indicate possible, not confirmed, injections.
    PII-only messages (no injection categories) are always kept because their content
    is already redacted and safe to include in LLM context.

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

    # Exclude injection messages (MEDIUM+ severity with injection categories).
    # PII-only messages are always kept: redacted/warned messages have safe content,
    # and blocked messages store a placeholder (not the original sensitive content).
    # __gt=[] uses PostgreSQL JSONB array comparison (non-empty array > empty array).
    # This filters for messages that have at least one injection category.
    injection_exclusion = Q(
        severity__in=EXCLUDED_SEVERITIES,
        injection_categories__gt=[],
    )
    filtered = all_messages.exclude(injection_exclusion).order_by("sequence_index")[
        :limit
    ]

    if logger.isEnabledFor(logging.DEBUG):
        total_excluded = all_messages.filter(injection_exclusion).count()
        if total_excluded:
            logger.debug(
                "Excluded %d message(s) from history for thread %s",
                total_excluded,
                thread.uuid,
            )

    return filtered


def build_context(user, user_input, thread=None) -> list[dict]:
    """
    Build the messages array for the LLM in OpenAI chat completions format.

    Assembles:
      1. System message (persona + tool usage guidelines + UI capabilities)
      2. Conversation history from DB (limited by LLM_CHAT_HISTORY_LIMIT, chronological)
      3. Current user message
    """
    if thread and thread.chat_session.user != user:
        raise PermissionDenied("Thread does not belong to the requesting user.")

    tools_prompt = tool_registry.get_tools_prompt()
    system_prompt = SYSTEM_PROMPT.format(tools=tools_prompt)

    messages = [{"role": "system", "content": system_prompt}]

    if thread:
        messages.extend(_get_conversation_history(thread))

    messages.append({"role": "user", "content": user_input})

    return messages


def _get_conversation_history(thread) -> list[dict]:
    db_messages = _get_thread_messages(thread)
    if db_messages is None or not db_messages.exists():
        return []
    result = []
    for role, content, tool_calls in db_messages.values_list(
        "role", "content", "tool_calls"
    ):
        if tool_calls and role == Message.Role.ASSISTANT:
            msg = {"role": role, "content": content or None}
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in tool_calls
            ]
            result.append(msg)
            # OpenAI requires tool role messages after assistant tool_calls
            for tc in tool_calls:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": "(result was displayed to user)",
                    }
                )
        elif content:
            result.append({"role": role, "content": content})
    return result


def build_rejection_input(thread) -> list[dict] | None:
    """Build messages array for context-aware rejection using recent conversation history.

    Returns None if no thread or no history (caller should fall back to static message).
    """
    if not thread:
        return None

    history = _get_conversation_history(thread)
    if not history:
        return None

    return [{"role": "system", "content": REJECTION_SYSTEM_PROMPT}, *history]
