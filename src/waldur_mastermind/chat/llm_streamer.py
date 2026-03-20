import json
import logging

import httpx
import openai
from constance import config
from django.db import transaction
from django.db.models import Max
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions

from waldur_mastermind.chat import models
from waldur_mastermind.chat.models import TokenQuota
from waldur_mastermind.chat.parsers import StreamParser
from waldur_mastermind.chat.prompts.rejection import TITLE_GENERATION_PROMPT
from waldur_mastermind.chat.providers import (
    ALLOWED_COMPLETION_KEYS,
    FALLBACK_DEFAULTS,
    PROVIDER_DEFAULTS,
)
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)


def validate_tool_call(tool_name, user):
    """Validates if the tool exists and user is authenticated."""
    if not user or not user.is_authenticated:
        raise rf_exceptions.NotAuthenticated()

    if tool_name not in tool_registry:
        raise rf_exceptions.ValidationError(
            {
                "tool": _("Tool '%(tool_name)s' is not recognized.")
                % {"tool_name": tool_name}
            }
        )


class LLMStreamer:
    """
    Handles the stateful logic of streaming and buffering NDJSON responses
    from an upstream LLM provider.

    Bandwidth optimizations:
    1. NDJSON Protocol: Removes 'data:' prefix and double newlines (SSE overhead).
    2. Short Keys: Uses single-char keys ('k', 'c') to minimize payload.
    3. Flattened Structure: Merges protocol fields with data fields.
    4. Compact JSON: Removes whitespace separators.
    5. Buffered Flushing: Reduces packet count by buffering text chunks.
    """

    def __init__(
        self,
        messages,
        url,
        token,
        user=None,
        thread=None,
        original_input="",
        is_new_thread=False,
        mode=None,
        user_msg=None,
        canned_response=None,
        pii_warning=None,
    ):
        self.messages = messages
        self.model = config.AI_ASSISTANT_MODEL
        self.backend_type = config.AI_ASSISTANT_BACKEND_TYPE
        _completion_kwargs = config.AI_ASSISTANT_COMPLETION_KWARGS
        self.completion_kwargs = (
            _completion_kwargs if isinstance(_completion_kwargs, dict) else {}
        )
        self.client = openai.OpenAI(
            api_key=token,
            base_url=url,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )
        self.tools = tool_registry.get_openai_tools()
        self.parser = StreamParser()
        self.accumulated_content = ""
        self.tool_calls: dict[int, dict] = {}
        self.user = user
        self.input_tokens = 0
        self.output_tokens = 0
        self.error = None
        self.thread = thread
        self.original_input = original_input
        self.is_new_thread = is_new_thread
        self.mode = mode
        self.user_msg = user_msg
        self._persisted_message_meta = None
        self._messages_persisted = False
        self.canned_response = canned_response
        self.pii_warning = pii_warning

    def _format_ndjson(self, data: dict) -> str:
        """Helper to format a dict as a Newline Delimited JSON line."""
        return f"{json.dumps(data, separators=(',', ':'))}\n"

    def _stream_completion(self, messages, include_tools=True):
        """Open a streaming chat completion and yield SDK chunk objects."""
        kwargs = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        # Layer 1: provider defaults
        provider_kwargs = PROVIDER_DEFAULTS.get(self.backend_type, FALLBACK_DEFAULTS)
        for key, value in provider_kwargs.items():
            kwargs[key] = value

        # Layer 2: admin overrides (allowlisted keys only)
        ignored_keys = set(self.completion_kwargs.keys()) - ALLOWED_COMPLETION_KEYS
        if ignored_keys:
            logger.warning(
                "AI_ASSISTANT_COMPLETION_KWARGS contains protected keys that will be ignored: %s",
                ignored_keys,
            )
        for key, value in self.completion_kwargs.items():
            if key not in ALLOWED_COMPLETION_KEYS:
                continue
            # Deep-merge extra_body so provider safety settings aren't wiped out
            if (
                key == "extra_body"
                and isinstance(kwargs.get("extra_body"), dict)
                and isinstance(value, dict)
            ):
                kwargs["extra_body"] = {**kwargs["extra_body"], **value}
            else:
                kwargs[key] = value

        if include_tools and self.tools:
            kwargs["tools"] = self.tools
            kwargs["parallel_tool_calls"] = False

        return self.client.chat.completions.create(**kwargs)

    def __iter__(self):
        if self.thread:
            yield self._format_ndjson({"m": {"thread_uuid": str(self.thread.uuid)}})

        # Yield PII warning as first content event (before LLM content)
        if self.pii_warning:
            yield self._format_ndjson({"w": self.pii_warning})

        self._messages_persisted = False

        try:
            # Blocked input: stream canned rejection, persist, and skip the LLM call
            if self.canned_response:
                self.accumulated_content = self.canned_response
                for block in self.parser.parse(self.canned_response):
                    yield self._format_ndjson(block)
                for block in self.parser.flush():
                    yield self._format_ndjson(block)
                self._persist_messages()
                if self._persisted_message_meta:
                    yield self._format_ndjson({"m": self._persisted_message_meta})
                self._generate_thread_name()
                return

            with self._stream_completion(self.messages) as stream:
                for chunk in stream:
                    if not chunk.choices:
                        # Final usage-only chunk
                        if chunk.usage:
                            self.input_tokens = chunk.usage.prompt_tokens or 0
                            self.output_tokens = chunk.usage.completion_tokens or 0
                        continue

                    delta = chunk.choices[0].delta

                    if delta.content:
                        self.accumulated_content += delta.content
                        for block in self.parser.parse(delta.content):
                            yield self._format_ndjson(block)

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            entry = self.tool_calls.setdefault(
                                tc.index,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            if tc.id:
                                entry["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    entry["name"] = tc.function.name
                                if tc.function.arguments:
                                    entry["arguments"] += tc.function.arguments

            for block in self.parser.flush():
                yield self._format_ndjson(block)

            # Execute any tool calls that were streamed
            if self.tool_calls and self.user:
                yield from self._execute_tool_calls(self.tool_calls)

            # Normal completion: persist and yield UUIDs
            self._persist_messages()
            if self._persisted_message_meta:
                yield self._format_ndjson({"m": self._persisted_message_meta})
            self._generate_thread_name()

        except openai.APIError as e:
            yield from self._handle_stream_error(e, "Upstream LLM request failed.")
        except Exception as e:
            yield from self._handle_stream_error(
                e,
                "Unexpected error during LLM streaming — this is a bug.",
                logging.CRITICAL,
            )

        finally:
            # Always record usage, even if stream was interrupted (GeneratorExit)
            self._record_usage()
            # Safety net for GeneratorExit - can't yield here
            if not self._messages_persisted:
                self._persist_messages()

    def _handle_stream_error(
        self, exc: Exception, log_msg: str, log_level: int = logging.ERROR
    ):
        """Log an error, emit an error NDJSON line, and persist messages."""
        logger.log(log_level, log_msg, exc_info=True)
        self.error = str(exc)
        yield self._format_ndjson(
            {"e": "Chat processing was interrupted. Please try again later."}
        )
        self._persist_messages()
        if self._persisted_message_meta:
            yield self._format_ndjson({"m": self._persisted_message_meta})

    def _execute_tool_calls(self, tool_calls: dict[int, dict]):
        """Execute all streamed tool calls and yield UI component results."""
        tool_executor = ToolExecutor(self.user)
        for entry in tool_calls.values():
            tool_name = entry["name"]
            try:
                arguments = json.loads(entry["arguments"]) if entry["arguments"] else {}
            except json.JSONDecodeError:
                logger.warning(
                    "Failed to parse tool call arguments for %s: %s",
                    tool_name,
                    entry["arguments"][:200],
                )
                continue

            logger.debug(
                "Executing tool call",
                extra={"tool_name": tool_name, "user_id": self.user.id},
            )
            result = tool_executor.execute_tool(tool_name, arguments)
            tool_block = self.parser.parse_tool_result(result)
            if tool_block:
                yield self._format_ndjson(tool_block)

    def _serialized_tool_calls(self) -> list[dict]:
        """Return tool calls in a clean format for DB storage."""
        result = []
        for entry in self.tool_calls.values():
            try:
                arguments = json.loads(entry["arguments"]) if entry["arguments"] else {}
            except json.JSONDecodeError:
                arguments = {}
            result.append(
                {"id": entry["id"], "name": entry["name"], "arguments": arguments}
            )
        return result

    def _persist_messages(self):
        """
        Save user and assistant messages to the thread.
        In reload/edit mode, replace the last assistant message.
        In edit mode, user message was pre-created in stream().
        """
        if not self.thread:
            return

        try:
            with transaction.atomic():
                locked_thread = models.ThreadSession.objects.select_for_update().get(
                    pk=self.thread.pk
                )

                persisted_user_msg = None
                persisted_assistant_msg = None
                effective_mode = self.mode

                if effective_mode in (models.ChatMode.RELOAD, models.ChatMode.EDIT):
                    # For EDIT mode, user message was pre-created in stream()
                    if effective_mode == models.ChatMode.EDIT and self.user_msg:
                        persisted_user_msg = self.user_msg

                    # Find last active assistant message to replace
                    last_assistant = (
                        locked_thread.messages.filter(
                            role=models.Message.Role.ASSISTANT,
                            replaced_by__isnull=True,
                        )
                        .order_by("-sequence_index")
                        .first()
                    )

                    if last_assistant:
                        # Create replacement with same sequence_index
                        persisted_assistant_msg = models.Message.objects.create(
                            thread=locked_thread,
                            role=models.Message.Role.ASSISTANT,
                            content=self.accumulated_content,
                            sequence_index=last_assistant.sequence_index,
                            replaces=last_assistant,
                            tool_calls=self._serialized_tool_calls(),
                        )
                    else:
                        # Fallback to normal mode if no assistant message found
                        logger.warning(
                            "%s mode requested but no assistant message found in thread %s, falling back to normal mode",
                            effective_mode,
                            self.thread.uuid,
                        )
                        effective_mode = None

                # Normal mode (or fallback from reload/edit)
                if effective_mode not in (models.ChatMode.RELOAD, models.ChatMode.EDIT):
                    if self.user_msg:
                        # User message was pre-created in the view
                        persisted_user_msg = self.user_msg
                    else:
                        last_index = (
                            locked_thread.messages.aggregate(Max("sequence_index"))[
                                "sequence_index__max"
                            ]
                            or 0
                        )
                        persisted_user_msg = models.Message.objects.create(
                            thread=locked_thread,
                            role=models.Message.Role.USER,
                            content=self.original_input,
                            sequence_index=last_index + 1,
                        )

                    persisted_assistant_msg = models.Message.objects.create(
                        thread=locked_thread,
                        role=models.Message.Role.ASSISTANT,
                        content=self.accumulated_content,
                        sequence_index=persisted_user_msg.sequence_index + 1,
                        tool_calls=self._serialized_tool_calls(),
                    )

                # Store UUIDs for metadata response
                self._persisted_message_meta = {}
                if persisted_user_msg:
                    self._persisted_message_meta["user_message_uuid"] = str(
                        persisted_user_msg.uuid
                    )
                if persisted_assistant_msg:
                    self._persisted_message_meta["assistant_message_uuid"] = str(
                        persisted_assistant_msg.uuid
                    )

                # Update thread's modified timestamp to reflect latest message
                locked_thread.save(update_fields=["modified"])

                self._messages_persisted = True

        except Exception as e:
            logger.error(
                "Failed to persist messages for thread %s: %s",
                self.thread.uuid,
                e,
                exc_info=True,
            )

    def _generate_thread_name(self):
        """
        Generate a short title for a new thread via a second LLM call.
        Updates the thread name in DB. Failures are logged but never break
        the main response.
        """
        if not self.is_new_thread or not self.thread or not self.original_input:
            return

        try:
            prompt = TITLE_GENERATION_PROMPT + self.original_input[:500]
            title_messages = [{"role": "user", "content": prompt}]
            title_parts = []

            with self._stream_completion(title_messages, include_tools=False) as stream:
                for chunk in stream:
                    if not chunk.choices:
                        if chunk.usage:
                            # Title generation is billed to the user's quota
                            # intentionally — it is LLM work done on their behalf.
                            self.input_tokens += chunk.usage.prompt_tokens or 0
                            self.output_tokens += chunk.usage.completion_tokens or 0
                        continue
                    content = chunk.choices[0].delta.content
                    if content:
                        title_parts.append(content)

            title = "".join(title_parts).strip().strip("\"'")
            if title:
                models.ThreadSession.objects.filter(pk=self.thread.pk).update(
                    name=title[:150]
                )

        except Exception:
            logger.exception("Failed to generate thread title for %s", self.thread.uuid)

    def _record_usage(self):
        """
        Atomically update token quota.
        Uses TokenQuota.for_user() for concurrent-safe updates.
        """
        if not self.user:
            return

        # Skip recording if no tokens were exchanged and no error occurred.
        # On error, we still record a zero-usage entry for audit visibility.
        if self.input_tokens == 0 and self.output_tokens == 0 and not self.error:
            return

        try:
            with transaction.atomic():
                quota = TokenQuota.for_user(self.user, True)

                total_tokens = self.input_tokens + self.output_tokens
                quota.add_usage(total_tokens)

                logger.info(
                    "Recorded AI usage for %s: input=%d, output=%d, daily usage=%d",
                    self.user.username,
                    self.input_tokens,
                    self.output_tokens,
                    quota.daily_usage,
                )

        except Exception as e:
            logger.error(
                "Failed to record AI usage for %s: %s",
                self.user.username,
                e,
                exc_info=True,
            )
