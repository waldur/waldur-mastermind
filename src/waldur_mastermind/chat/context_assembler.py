import json
import logging
from dataclasses import dataclass

from constance import config
from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from waldur_mastermind.chat.input_guards import SeverityLevel
from waldur_mastermind.chat.intent_classifier import Intent, classify_intent
from waldur_mastermind.chat.models import Message
from waldur_mastermind.chat.prompts.assembly import SYSTEM_PROMPT_TEMPLATE
from waldur_mastermind.chat.prompts.rejection import REJECTION_SYSTEM_PROMPT_TEMPLATE
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user

logger = logging.getLogger(__name__)

# Messages with injection at MEDIUM severity or above are excluded from AI Assistant context history.
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
    is already redacted and safe to include in AI Assistant context.

    Returns None if history limit is invalid, empty queryset if no messages.
    """
    limit = config.AI_ASSISTANT_HISTORY_LIMIT
    if not isinstance(limit, int) or limit <= 0:
        logger.warning(
            "Invalid AI_ASSISTANT_HISTORY_LIMIT value: %s.",
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


def build_context(
    user,
    user_input,
    thread=None,
    *,
    include_tools_prompt=True,
    history=None,
) -> list[dict]:
    """
    Build the messages array for the AI Assistant in OpenAI chat completions format.

    Assembles:
      1. System message (persona + tool usage guidelines + UI capabilities)
      2. Conversation history from DB (limited by AI_ASSISTANT_HISTORY_LIMIT, chronological)
      3. Current user message

    Args:
        include_tools_prompt: When False, omit tool usage guidelines from the
            system prompt. Used when the intent classifier determines tools
            will not be sent to the LLM.
        history: Pre-fetched conversation history. When provided, skips the
            DB query for history. Used by ``build_context_with_intent`` to
            avoid duplicate DB queries.
    """
    if thread and thread.chat_session.user != user:
        raise PermissionDenied("Thread does not belong to the requesting user.")

    tool_names = get_tool_set_for_user(user) if include_tools_prompt else None
    tools_prompt = (
        tool_registry.get_tools_prompt(tool_names) if include_tools_prompt else ""
    )
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        tools=tools_prompt,
        assistant_name=config.AI_ASSISTANT_NAME,
        organization=config.SITE_NAME,
    )

    messages = [{"role": "system", "content": system_prompt}]

    if thread:
        if history is not None:
            messages.extend(history)
        else:
            messages.extend(_get_conversation_history(thread))

    messages.append({"role": "user", "content": user_input})

    return messages


@dataclass
class ContextResult:
    """Result of context assembly with classified intent."""

    messages: list[dict]
    intent: Intent


def build_context_with_intent(user, user_input, thread=None) -> ContextResult:
    """Build context messages and classify user intent.

    Fetches conversation history once and reuses it for both intent
    classification and context assembly (avoids a duplicate DB query).
    When the classified intent suppresses tools, tool usage guidelines
    are also omitted from the system prompt.
    """
    history = _get_conversation_history(thread) if thread else []
    intent = classify_intent(user_input, history)
    logger.info(
        "Intent classified: %s (include_tools=%s) for input: %.80s",
        intent.value,
        intent.include_tools,
        user_input,
    )
    messages = build_context(
        user,
        user_input,
        thread,
        include_tools_prompt=intent.include_tools,
        history=history,
    )
    return ContextResult(messages=messages, intent=intent)


def _get_conversation_history(thread) -> list[dict]:
    db_messages = _get_thread_messages(thread)
    if db_messages is None or not db_messages.exists():
        return []
    result: list[dict] = []
    for msg in db_messages:
        result.extend(blocks_to_llm_messages(msg))
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

    rejection_prompt = REJECTION_SYSTEM_PROMPT_TEMPLATE.format(
        organization=config.SITE_NAME,
    )
    return [{"role": "system", "content": rejection_prompt}, *history]


# -------- blocks -> LLM messages --------

_FENCED_KINDS = {"code", "mermaid"}
_TEXT_KINDS = {"markdown"} | _FENCED_KINDS


def _block_to_text(block: dict) -> str:
    """Render a text-bearing block as the string the LLM will see."""
    key = block["key"]
    content = block.get("content", "")
    if key == "code":
        return f"```{block.get('tag', '')}\n{content}\n```"
    if key == "mermaid":
        return f"```mermaid\n{content}\n```"
    return content  # markdown


def blocks_to_llm_messages(message) -> list[dict]:
    """Convert Message.blocks into an OpenAI-style message list.

    Text-bearing blocks (markdown/code/mermaid) accumulate into a single
    role=<message.role> text entry. Tool blocks combine any pending text
    into the `content` field of the assistant tool_calls entry — this is
    the canonical OpenAI shape the model itself emits when calling tools
    with accompanying prose. The tool_calls entry is followed by a
    role=tool reply built from the tool summary. Any trailing text after
    a tool block flushes as its own role=assistant message.
    """
    result: list[dict] = []
    text_buf: list[str] = []

    def _drain_text() -> str:
        content = "\n\n".join(text_buf) if text_buf else ""
        text_buf.clear()
        return content

    def _flush_text_msg():
        content = _drain_text()
        if content:
            result.append({"role": message.role, "content": content})

    for block in message.blocks or []:
        key = block.get("key")
        if key in _TEXT_KINDS:
            text_buf.append(_block_to_text(block))
        elif key == "tool":
            # Defensive: skip malformed tool blocks missing the `tool`
            # metadata sub-dict. This can happen for rows persisted before
            # `_finalize_current_block` learned to drop loading tool blocks
            # that never received a result (hidden tool errors, cancellation).
            # We skip rather than synthesize a placeholder because emitting a
            # tool_calls entry without a matching tool-role reply would leave
            # the LLM context in an invalid state.
            tool = block.get("tool")
            if not tool or not tool.get("call_id"):
                logger.warning(
                    "Skipping malformed tool block in message %s: missing tool metadata",
                    getattr(message, "uuid", "?"),
                )
                continue
            # Combine pending text onto the tool_calls message (OpenAI
            # canonical shape). content=None when there's no preceding text.
            pending = _drain_text()
            result.append(
                {
                    "role": "assistant",
                    "content": pending or None,
                    "tool_calls": [
                        {
                            "id": tool["call_id"],
                            "type": "function",
                            "function": {
                                "name": tool.get("name", ""),
                                "arguments": json.dumps(tool.get("arguments") or {}),
                            },
                        }
                    ],
                }
            )
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": tool["call_id"],
                    "content": tool.get("summary", ""),
                }
            )
        # vm_order blocks without a tool wrapper are unusual -- skip them
        # in LLM context (they would only appear if the assistant emitted
        # a standalone structured block, which our streamer does not
        # currently do).
    _flush_text_msg()
    return result
