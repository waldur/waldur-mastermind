import json
import logging
import queue
import threading
import time

import httpx
import openai
from constance import config
from django.db import connections, transaction
from django.db.models import Max
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions

from waldur_mastermind.chat import models
from waldur_mastermind.chat.block_schemas import blocks_to_text
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
from waldur_mastermind.chat.tools.tool_sets import get_tool_set_for_user

logger = logging.getLogger(__name__)

# Maps specific OpenAI exception types to user-facing error messages.
# Looked up by exact type in the `except openai.APIError` handler;
# unmatched subclasses fall back to a generic message.
_LLM_ERROR_MESSAGES: dict[type, str] = {
    openai.AuthenticationError: "AI service authentication failed. Please contact your administrator.",
    openai.RateLimitError: "AI service rate limit reached. Please wait and try again.",
    openai.APITimeoutError: "AI service request timed out. Please try again.",
    openai.APIConnectionError: "Could not connect to AI service. Please try again later.",
    openai.InternalServerError: "AI service is temporarily unavailable. Please try again later.",
    openai.NotFoundError: "AI model not found. Please check the configured model name.",
    openai.BadRequestError: "AI service rejected the request. Please try again or contact your administrator.",
}

# Maximum time (seconds) the consumer loop waits for the worker to finish.
_WORKER_TIMEOUT = 300

# Maximum number of NDJSON items buffered between the worker and consumer threads.
_QUEUE_MAXSIZE = 256

# How often (in content chunks) the worker polls the DB for a cross-process
# cancel signal.  A single PK lookup costs ~0.1 ms, so checking every 5
# chunks adds negligible overhead.
_CANCEL_CHECK_INTERVAL = 5


class _StreamDone:
    """Sentinel placed on queue to signal the worker has finished."""


class _StreamError:
    """Placed on queue when the worker encounters an error.

    Carries pre-formatted NDJSON error line(s) for the client.
    """

    def __init__(self, ndjson_lines: list[str]):
        self.ndjson_lines = ndjson_lines


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

    The LLM HTTP call runs in a background thread so that a client disconnect
    does not abort the upstream connection. The full response is always
    received and persisted regardless of client state.

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
        assistant_msg=None,
        canned_response=None,
        pii_warning=None,
        intent=None,
    ):
        self.messages = messages
        self.client = openai.OpenAI(
            api_key=token,
            base_url=url,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )
        self.parser = StreamParser()
        self.accumulated_blocks: list[dict] = []
        self._current_block: dict | None = None
        self.accumulated_warning: str = ""
        self.pending_tool_calls: dict[str, dict] = {}
        self.tool_calls: dict[int, dict] = {}
        self.user = user
        self.input_tokens = None
        self.output_tokens = None
        self.error = None
        self.thread = thread
        self.original_input = original_input
        self.is_new_thread = is_new_thread
        self.mode = mode
        self.user_msg = user_msg
        self.assistant_msg = assistant_msg
        self._persisted_message_meta = None
        self.canned_response = canned_response
        self.pii_warning = pii_warning
        self.intent = intent

        # Thread-based streaming infrastructure
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._client_gone = threading.Event()
        self._cancel_requested = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._chunk_count = 0
        self._persisted = False
        self._flushed = False

    def _format_ndjson(self, data: dict) -> str:
        """Helper to format a dict as a Newline Delimited JSON line."""
        return f"{json.dumps(data, separators=(',', ':'))}\n"

    def _stream_completion(self, messages, include_tools=True):
        """Open a streaming chat completion and yield SDK chunk objects."""
        model = config.AI_ASSISTANT_MODEL
        backend_type = config.AI_ASSISTANT_BACKEND_TYPE
        _completion_kwargs = config.AI_ASSISTANT_COMPLETION_KWARGS
        completion_kwargs = (
            _completion_kwargs if isinstance(_completion_kwargs, dict) else {}
        )
        user_tools = get_tool_set_for_user(self.user)

        # Semantic routing: pre-filter tools based on query similarity.
        # Falls back to all user tools when fastembed is not available.
        from waldur_mastermind.chat.semantic_routing import get_relevant_tools

        user_message = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        relevant_tools = get_relevant_tools(user_message, user_tools)
        tools = tool_registry.get_openai_tools(relevant_tools)

        kwargs = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        # Layer 1: provider defaults
        provider_kwargs = PROVIDER_DEFAULTS.get(backend_type, FALLBACK_DEFAULTS)
        for key, value in provider_kwargs.items():
            kwargs[key] = value

        # Layer 2: admin overrides (allowlisted keys only)
        ignored_keys = set(completion_kwargs.keys()) - ALLOWED_COMPLETION_KEYS
        if ignored_keys:
            logger.warning(
                "AI_ASSISTANT_COMPLETION_KWARGS contains protected keys that will be ignored: %s",
                ignored_keys,
            )
        for key, value in completion_kwargs.items():
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

        if include_tools and tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = False

        return self.client.chat.completions.create(**kwargs)

    @property
    def _stopped(self):
        """True when we should stop producing new content (disconnect or cancel)."""
        return self._client_gone.is_set() or self._cancel_requested.is_set()

    def _enqueue(self, item):
        """Put an item on the consumer queue, discarding only when the client
        has disconnected so the worker thread is never blocked on a full queue
        that nobody drains. On cancel the client is still reading the stream,
        so we keep enqueuing final frames (cancel marker, persisted meta,
        _StreamDone)."""
        while not self._client_gone.is_set():
            try:
                self._queue.put(item, timeout=0.5)
                return
            except queue.Full:
                continue
        # Client is gone; silently discard.

    def _check_cancelled(self):
        """Check if cancellation was requested or this stream was superseded.

        Detects two cross-process conditions:
        1. Explicit cancel: cancel_requested_at is set (user clicked stop)
        2. Superseded: a newer user message exists on the thread (new message arrived)
        """
        if self._cancel_requested.is_set():
            return True
        self._chunk_count += 1
        if self._chunk_count % _CANCEL_CHECK_INTERVAL == 0 and self.thread:
            # Check 1: explicit cancel via stop button
            if models.ThreadSession.objects.filter(
                pk=self.thread.pk,
                cancel_requested_at__isnull=False,
            ).exists():
                logger.info(
                    "Cross-process cancel detected for thread %s",
                    self.thread.pk,
                )
                self._cancel_requested.set()
                return True

            # Check 2: superseded by a newer message on this thread
            if (
                self.user_msg
                and models.Message.objects.filter(
                    thread_id=self.thread.pk,
                    role=models.Message.Role.USER,
                    sequence_index__gt=self.user_msg.sequence_index,
                    replaced_by__isnull=True,
                ).exists()
            ):
                logger.info(
                    "Stream superseded for thread %s (newer user message exists)",
                    self.thread.pk,
                )
                self._cancel_requested.set()
                return True
        return False

    def _absorb_block(self, chunk: dict) -> None:
        """Update accumulated_blocks / accumulated_warning from a parsed chunk.

        Chunks are the same dicts the streamer already yields to the frontend
        (via self.parser.parse()). This method keeps a finalized blocks list
        mirroring the wire stream so persistence can write Message.blocks as-is.
        """
        if "w" in chunk:
            self.accumulated_warning = chunk["w"]
            return

        kind = chunk.get("k")
        call_id = chunk.get("call_id")

        if kind == "load":
            self._finalize_current_block()
            self._current_block = {
                "id": self._next_block_id(),
                "key": "tool",
                "status": "loading",
                "_pending_call_id": call_id,
            }
            return

        if (
            self._current_block is not None
            and self._current_block.get("key") == "tool"
            and self._current_block.get("status") == "loading"
            and call_id
            and self._current_block.get("_pending_call_id") == call_id
        ):
            result_block = self._chunk_to_block(
                chunk, blk_id=f"{self._current_block['id']}_r"
            )
            tool_meta = (self.pending_tool_calls or {}).get(call_id, {})
            self._current_block = {
                "id": self._current_block["id"],
                "key": "tool",
                "status": "complete",
                "tool": {
                    "call_id": call_id,
                    "name": tool_meta.get("name", ""),
                    "arguments": tool_meta.get("arguments") or {},
                    "summary": tool_meta.get("summary", ""),
                },
                "result": result_block,
            }
            self._finalize_current_block()
            return

        if kind in ("markdown", "code", "mermaid"):
            if (
                self._current_block is not None
                and self._current_block.get("key") == kind
                and self._current_block.get("status") != "loading"
            ):
                self._current_block["content"] += chunk.get("c", "")
            else:
                self._finalize_current_block()
                self._current_block = self._chunk_to_block(
                    chunk, blk_id=self._next_block_id()
                )
            return

        if kind == "vm_order":
            self._finalize_current_block()
            self._current_block = self._chunk_to_block(
                chunk, blk_id=self._next_block_id()
            )
            self._finalize_current_block()
            return

        logger.warning(
            "Unknown block kind in accumulator: kind=%r chunk_keys=%r",
            kind,
            sorted(chunk.keys()),
        )

    def _finalize_current_block(self) -> None:
        """Move _current_block into accumulated_blocks with status=complete.

        Loading tool blocks that never received a result (hidden tool errors,
        mid-stream cancellation, or arg-parse failures) are dropped instead of
        persisted.  Persisting them would yield a ``key=="tool"`` block with no
        ``tool`` sub-dict, which later crashes context assembly.
        """
        if self._current_block is None:
            return
        if (
            self._current_block.get("key") == "tool"
            and self._current_block.get("status") == "loading"
        ):
            self._current_block = None
            return
        blk = {k: v for k, v in self._current_block.items() if not k.startswith("_")}
        blk["status"] = "complete"
        self.accumulated_blocks.append(blk)
        self._current_block = None

    def _next_block_id(self) -> str:
        """Return the next stable block id.

        Invariant: ids are monotonically increasing per absorbed block.
        `len(accumulated_blocks)` is the count of finalized blocks; the
        `+1` reserves the slot occupied by `_current_block` if one is
        open but not yet finalized. This relies on `_finalize_current_block`
        always nulling `_current_block` after append — never reuse a
        finalized block.
        """
        return f"blk_{len(self.accumulated_blocks) + (1 if self._current_block else 0)}"

    def _chunk_to_block(self, chunk: dict, blk_id: str) -> dict:
        """Normalize a wire chunk dict into a persisted block dict."""
        kind = chunk.get("k", "markdown")
        base: dict = {"id": blk_id, "key": kind, "status": "complete"}
        if kind in ("markdown", "code", "mermaid"):
            base["content"] = chunk.get("c", "")
            if "t" in chunk and kind == "code":
                base["tag"] = chunk["t"]
        elif kind == "vm_order":
            for field in (
                "order_id",
                "name",
                "flavor",
                "image",
                "project",
                "organization",
                "project_uuid",
                "order_status",
                "message",
                "error",
                "flavors",
                "images",
                "projects",
                "offerings",
            ):
                if field in chunk:
                    base[field] = chunk[field]
        elif kind == "resource_list":
            for field in (
                "project_uuid",
                "customer_uuid",
                "category_uuid",
                "state",
            ):
                if field in chunk:
                    base[field] = chunk[field]
        elif kind == "homeport_nav":
            for field in ("links", "content"):
                if field in chunk:
                    base[field] = chunk[field]
        return base

    def _flush_parser(self):
        """Flush remaining parser content to the queue. Idempotent.

        Also finalizes any in-progress accumulator block — by the time
        the parser flushes, the stream is done and no further deltas can
        extend the current block.
        """
        if self._flushed:
            return
        self._flushed = True
        for block in self.parser.flush():
            self._absorb_block(block)
            self._enqueue(self._format_ndjson(block))
        self._finalize_current_block()

    @staticmethod
    def _parse_arguments(raw: str) -> dict | None:
        """Parse tool call JSON arguments. Returns None on parse failure."""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _accumulate_tool_call_delta(self, tc):
        """Merge a single streamed tool-call delta into self.tool_calls."""
        entry = self.tool_calls.setdefault(
            tc.index, {"id": "", "name": "", "arguments": ""}
        )
        if tc.id:
            entry["id"] = tc.id
        if tc.function:
            if tc.function.name:
                entry["name"] = tc.function.name
            if tc.function.arguments:
                entry["arguments"] += tc.function.arguments

    def __iter__(self):
        if self.thread:
            yield self._format_ndjson({"m": {"thread_uuid": str(self.thread.uuid)}})

        # Yield PII warning as first content event (before LLM content)
        if self.pii_warning:
            self._absorb_block({"w": self.pii_warning})
            yield self._format_ndjson({"w": self.pii_warning})

        # Blocked input: stream canned rejection synchronously (no LLM call)
        if self.canned_response:
            for block in self.parser.parse(self.canned_response):
                self._absorb_block(block)
                yield self._format_ndjson(block)
            # Drain the remaining parser buffer and finalize any in-progress
            # block. We can't use _flush_parser() here because it enqueues
            # frames on self._queue, but the canned path yields directly.
            self._flushed = True
            for block in self.parser.flush():
                self._absorb_block(block)
                yield self._format_ndjson(block)
            self._finalize_current_block()
            self._persist_messages()
            if self._persisted_message_meta:
                yield self._format_ndjson({"m": self._persisted_message_meta})
            self._generate_thread_name()
            self._record_usage()
            return

        # Start worker thread for LLM streaming
        self._worker_thread = threading.Thread(
            target=self._llm_worker,
            name="llm-streamer-worker",
            daemon=True,
        )
        self._worker_thread.start()

        # Consume queue and yield to client
        start = time.monotonic()
        try:
            while True:
                try:
                    item = self._queue.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - start > _WORKER_TIMEOUT:
                        logger.error(
                            "LLM worker thread exceeded %ds timeout", _WORKER_TIMEOUT
                        )
                        yield self._format_ndjson(
                            {"e": "Request timed out. Please try again."}
                        )
                        self._client_gone.set()
                        return
                    continue

                if isinstance(item, _StreamDone):
                    break
                elif isinstance(item, _StreamError):
                    yield from item.ndjson_lines
                    # Continue draining — _StreamDone arrives after the
                    # worker's finally block finishes all DB operations.
                else:
                    yield item
                    start = time.monotonic()
        except GeneratorExit:
            # Client disconnected. Signal worker but let it finish on its own.
            self._client_gone.set()
            return
        except Exception:
            # Unexpected error — let the worker handle DB ops.
            self._client_gone.set()
            raise

    def _llm_worker(self):
        """Background thread: consume LLM stream, enqueue NDJSON.

        All DB operations (persist, thread naming, usage recording) are
        handled here in the ``finally`` block, keeping persistence in a
        single code path regardless of whether the client is still connected.
        """
        try:
            self._run_llm_workflow()
        except openai.APIError as e:
            user_msg = _LLM_ERROR_MESSAGES.get(
                type(e),
                "AI service encountered an error. Please try again later.",
            )
            logger.error("Upstream LLM request failed.", exc_info=True)
            self.error = str(e)
            self._enqueue(_StreamError([self._format_ndjson({"e": user_msg})]))
        except Exception as e:
            logger.critical(
                "Unexpected error during LLM streaming — this is a bug.",
                exc_info=True,
            )
            self.error = str(e)
            self._enqueue(
                _StreamError(
                    [
                        self._format_ndjson(
                            {
                                "e": "Chat processing was interrupted. Please try again later."
                            }
                        )
                    ]
                )
            )
        finally:
            # Flush any buffered parser content / in-progress block so
            # accumulated_blocks reflects everything the stream produced.
            # Safe to call even when the normal flush path already ran
            # (idempotent via self._flushed).
            self._flush_parser()
            # Always persist — worker is the single owner of DB operations.
            self._persist_messages()
            # Title-gen tokens are stored on ThreadSession.title_gen_tokens
            # and added to self.input_tokens/output_tokens for quota recording.
            self._generate_thread_name()
            self._record_usage()
            # Enqueue metadata for the client before signaling completion.
            # If client is gone, _enqueue silently discards — that's fine.
            if self._persisted_message_meta:
                self._enqueue(self._format_ndjson({"m": self._persisted_message_meta}))
            self._enqueue(_StreamDone())
            # Clean up DB connections owned by this thread.
            # Skip when running synchronously on the main thread (e.g. tests)
            # to avoid destroying the caller's connection.
            if threading.current_thread() is not threading.main_thread():
                connections.close_all()

    def _run_llm_workflow(self):
        """Execute the full LLM streaming workflow (runs in worker thread).

        Implements the standard tool-use loop: if the LLM emits tool_calls,
        we execute them, append the results as ``tool`` role messages, and
        make a follow-up LLM call so the model can generate a natural-language
        response that references the tool data.
        """
        include_tools = self.intent.include_tools if self.intent else True
        self._stream_and_collect(self.messages, include_tools=include_tools)

        # Tool-use follow-up: if the LLM called tools, execute them and
        # give the LLM a second pass to generate a user-facing response.
        if self.tool_calls and self.user and not self._stopped:
            self._execute_tool_calls_worker(self.tool_calls)

            # Build follow-up messages: original context + assistant tool_call
            # + tool results with full structured data for the LLM to reference.
            followup_messages = list(self.messages)
            followup_messages.append(
                {
                    "role": "assistant",
                    "content": blocks_to_text(self.accumulated_blocks) or None,
                    "tool_calls": [
                        {
                            "id": entry["id"],
                            "type": "function",
                            "function": {
                                "name": entry["name"],
                                "arguments": entry["arguments"],
                            },
                        }
                        for entry in self.tool_calls.values()
                    ],
                }
            )
            for entry in self.tool_calls.values():
                # Send the full data dict (not just summary) so the LLM can
                # generate a rich, detailed response from the tool output.
                result_data = entry.get("_result_data", {})
                summary = entry.get("_summary", "Done")
                tool_content = json.dumps(result_data) if result_data else summary
                followup_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": entry["id"],
                        "content": tool_content,
                    }
                )

            # Reset flush guard so the second stream's trailing parser buffer
            # can be flushed by _stream_and_collect -> _flush_parser().
            self._flushed = False
            # Second LLM call — no tools this time, just generate text.
            self._stream_and_collect(followup_messages, include_tools=False)

    def _stream_and_collect(self, messages, include_tools=True):
        """Stream one LLM completion, accumulating content and tool calls."""
        with self._stream_completion(messages, include_tools=include_tools) as stream:
            for chunk in stream:
                if chunk.usage:
                    self.input_tokens = (self.input_tokens or 0) + (
                        chunk.usage.prompt_tokens or 0
                    )
                    self.output_tokens = (self.output_tokens or 0) + (
                        chunk.usage.completion_tokens or 0
                    )

                if not chunk.choices:
                    continue

                if self._check_cancelled():
                    if not self._persisted:
                        self._persist_on_cancel()
                    continue

                delta = chunk.choices[0].delta

                if delta.content:
                    for block in self.parser.parse(delta.content):
                        self._absorb_block(block)
                        self._enqueue(self._format_ndjson(block))

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        self._accumulate_tool_call_delta(tc)

        self._flush_parser()

    def _execute_tool_calls_worker(self, tool_calls: dict[int, dict]):
        """Execute all streamed tool calls and enqueue UI component results."""
        tool_executor = ToolExecutor(self.user)

        # Build call_id → metadata lookup for the blocks accumulator. The
        # wire chunks do not carry call_id (to preserve the frontend
        # protocol), so we inject it into local copies below and have the
        # accumulator look up name/arguments/summary via this dict.
        self.pending_tool_calls = {
            entry["id"]: {
                "name": entry["name"],
                "arguments": self._parse_arguments(entry["arguments"]) or {},
                "summary": "",
            }
            for entry in tool_calls.values()
            if entry.get("id")
        }

        for entry in tool_calls.values():
            tool_name = entry["name"]
            call_id = entry.get("id", "")
            arguments = self._parse_arguments(entry["arguments"])
            if arguments is None:
                logger.warning(
                    "Failed to parse tool call arguments for %s: %s",
                    tool_name,
                    entry["arguments"][:200],
                )
                continue

            # Emit loading indicator so the frontend can show an inline spinner
            load_chunk = {"k": "load", "t": "tool"}
            self._absorb_block({**load_chunk, "call_id": call_id})
            self._enqueue(self._format_ndjson(load_chunk))

            logger.debug(
                "Executing tool call",
                extra={"tool_name": tool_name, "user_id": self.user.id},
            )
            result = tool_executor.execute_tool(tool_name, arguments)
            tool_block = self.parser.parse_tool_result(result)

            # Store result for DB persistence (None for hidden errors)
            entry["_result_block"] = tool_block
            entry["_summary"] = result.get("summary", "")
            # Store full data for LLM follow-up call
            entry["_result_data"] = result.get("data", {})

            # Keep accumulator metadata in sync so the tool block has the
            # correct summary when the result chunk is folded in.
            if call_id in self.pending_tool_calls:
                self.pending_tool_calls[call_id]["summary"] = result.get("summary", "")

            if tool_block:
                self._absorb_block({**tool_block, "call_id": call_id})
                self._enqueue(self._format_ndjson(tool_block))

    def _persist_on_cancel(self):
        """Flush parser and persist partial content immediately on cancel.

        Called from the stream loop at the moment cancellation is first
        detected, so the partial response is saved before any new message
        can arrive on this thread.  The worker continues draining the LLM
        stream afterward to capture the usage-only chunk for accurate
        token accounting.
        """
        self._flush_parser()
        self._persist_messages()

    def _persist_messages(self):
        """Save user and assistant messages to the thread.

        In reload/edit mode, replace the last assistant message.
        In edit mode, user message was pre-created in stream().

        Called once per request: from the worker thread's ``finally`` block
        (LLM path) or inline in ``__iter__`` (canned response path).
        """
        if not self.thread:
            return
        if self._persisted:
            return

        # Defensive: the worker's finally block already flushes the parser,
        # but _persist_on_cancel / canned-response paths bypass that, so
        # guarantee accumulated_blocks is fully materialized before we hit
        # the database. Idempotent via self._flushed.
        self._flush_parser()

        try:
            with transaction.atomic():
                locked_thread = models.ThreadSession.objects.select_for_update().get(
                    pk=self.thread.pk
                )

                user_msg, assistant_msg = None, None

                if self.mode in (models.ChatMode.RELOAD, models.ChatMode.EDIT):
                    user_msg, assistant_msg = self._persist_reload_or_edit(
                        locked_thread
                    )

                if assistant_msg is None:
                    user_msg, assistant_msg = self._persist_normal(
                        locked_thread, user_msg
                    )

                self._finalize_thread(locked_thread, user_msg, assistant_msg)

        except Exception as e:
            logger.error(
                "Failed to persist messages for thread %s: %s",
                self.thread.uuid,
                e,
                exc_info=True,
            )
        else:
            self._persisted = True

    def _resolve_assistant_placeholder(self):
        """Finalize the pre-created assistant placeholder.

        On error-before-content the placeholder is deleted so the original
        message revives (SET_NULL on ``replaces``).  Otherwise, the
        placeholder is updated with the accumulated blocks.
        """
        # Finalize any block still streaming.
        self._finalize_current_block()

        if self.error and not self.accumulated_blocks:
            self.assistant_msg.delete()
            return None

        self.assistant_msg.blocks = self.accumulated_blocks
        self.assistant_msg.warning = self.accumulated_warning
        self.assistant_msg.input_tokens = self.input_tokens
        self.assistant_msg.output_tokens = self.output_tokens
        self.assistant_msg.save(
            update_fields=[
                "blocks",
                "warning",
                "input_tokens",
                "output_tokens",
                "modified",
            ]
        )
        return self.assistant_msg

    def _persist_reload_or_edit(self, locked_thread):
        """Handle RELOAD/EDIT persistence.

        Returns (user_msg, assistant_msg). If no last assistant message is
        found, returns (user_msg, None) to signal fallback to normal mode.
        """
        user_msg = None

        # For EDIT mode, user message was pre-created in stream()
        if self.mode == models.ChatMode.EDIT and self.user_msg:
            user_msg = self.user_msg

        # Pre-created placeholder (replaces the old assistant) — update in place
        if self.assistant_msg:
            return user_msg, self._resolve_assistant_placeholder()

        # No placeholder means no assistant message existed to replace —
        # fall back to normal mode.
        logger.warning(
            "%s mode requested but no assistant message found in thread %s, falling back to normal mode",
            self.mode,
            self.thread.uuid,
        )
        return user_msg, None

    def _persist_normal(self, locked_thread, existing_user_msg):
        """Handle NORMAL persistence.

        Returns (user_msg, assistant_msg).
        """
        if existing_user_msg:
            user_msg = existing_user_msg
        elif self.user_msg:
            # User message was pre-created in the view
            user_msg = self.user_msg
        else:
            last_index = (
                locked_thread.messages.aggregate(Max("sequence_index"))[
                    "sequence_index__max"
                ]
                or 0
            )
            user_msg = models.Message.objects.create(
                thread=locked_thread,
                role=models.Message.Role.USER,
                blocks=[
                    {
                        "id": "blk_0",
                        "key": "markdown",
                        "status": "complete",
                        "content": self.original_input,
                    }
                ],
                sequence_index=last_index + 1,
            )

        if self.assistant_msg:
            assistant_msg = self._resolve_assistant_placeholder()
        else:
            # Finalize any block still streaming before persisting.
            self._finalize_current_block()
            assistant_msg = models.Message.objects.create(
                thread=locked_thread,
                role=models.Message.Role.ASSISTANT,
                blocks=self.accumulated_blocks,
                warning=self.accumulated_warning,
                sequence_index=user_msg.sequence_index + 1,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
            )
        return user_msg, assistant_msg

    def _finalize_thread(self, locked_thread, user_msg, assistant_msg):
        """Store message UUIDs for metadata response and update thread."""
        self._persisted_message_meta = {}
        if user_msg:
            self._persisted_message_meta["user_message_uuid"] = str(user_msg.uuid)
        if assistant_msg:
            self._persisted_message_meta["assistant_message_uuid"] = str(
                assistant_msg.uuid
            )

        # Update thread's modified timestamp and clear cancel flag
        # atomically so a new request on this thread won't see a
        # stale cancel signal.
        locked_thread.cancel_requested_at = None
        locked_thread.save(update_fields=["modified", "cancel_requested_at"])

    def _title_source_text(self) -> str:
        """Return the user text to use as title generation input.

        Prefers blocks[0]['content'] when the persisted user_msg is available,
        falling back to original_input for:
        - the canned-response path where user_msg is never created;
        - user_msg with empty blocks;
        - first block that is non-textual or has empty content (e.g. a
          vm_order block, where .get("content") returns "" or None).
        For the normal markdown path, both sources are equal by construction,
        so this is a blocks-first cleanup with no behavior change.
        """
        if self.user_msg and self.user_msg.blocks:
            content = self.user_msg.blocks[0].get("content") or ""
            if content:
                return content
        return self.original_input

    def _generate_thread_name(self):
        """
        Generate a short title for a new thread via a second LLM call.
        Updates the thread name and title_gen_tokens in DB.
        Failures are logged but never break the main response.
        """
        source_text = self._title_source_text()
        if not self.is_new_thread or not self.thread or not source_text:
            return

        try:
            prompt = TITLE_GENERATION_PROMPT + source_text[:500]
            title_messages = [{"role": "user", "content": prompt}]
            title_parts = []
            title_input = 0
            title_output = 0

            with self._stream_completion(title_messages, include_tools=False) as stream:
                for chunk in stream:
                    if chunk.usage:
                        title_input += chunk.usage.prompt_tokens or 0
                        title_output += chunk.usage.completion_tokens or 0

                    if not chunk.choices:
                        continue
                    content = chunk.choices[0].delta.content
                    if content:
                        title_parts.append(content)

            title = "".join(title_parts).strip().strip("\"'")

            update_kwargs = {}
            if title:
                update_kwargs["name"] = title[:150]
            if title_input or title_output:
                update_kwargs["title_gen_input_tokens"] = title_input
                update_kwargs["title_gen_output_tokens"] = title_output
            if update_kwargs:
                models.ThreadSession.objects.filter(pk=self.thread.pk).update(
                    **update_kwargs
                )

            # Add to self for quota recording
            self.input_tokens = (self.input_tokens or 0) + title_input
            self.output_tokens = (self.output_tokens or 0) + title_output

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
        if not self.input_tokens and not self.output_tokens and not self.error:
            return

        try:
            with transaction.atomic():
                quota = TokenQuota.for_user(self.user, True)

                total_tokens = (self.input_tokens or 0) + (self.output_tokens or 0)
                quota.add_usage(total_tokens)

                logger.info(
                    "Recorded AI usage for %s: input=%d, output=%d, daily usage=%d",
                    self.user.username,
                    self.input_tokens or 0,
                    self.output_tokens or 0,
                    quota.daily_usage,
                )

        except Exception as e:
            logger.error(
                "Failed to record AI usage for %s: %s",
                self.user.username,
                e,
                exc_info=True,
            )
