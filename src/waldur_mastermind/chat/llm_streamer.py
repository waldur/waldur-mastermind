import json
import logging
import queue
import threading
import time

import httpx
from constance import config
from django.db import connections, transaction
from django.db.models import Max
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions

from waldur_mastermind.chat import models
from waldur_mastermind.chat.block_schemas import blocks_to_text, clean_answer_blocks
from waldur_mastermind.chat.models import TokenQuota
from waldur_mastermind.chat.parsers import StreamParser
from waldur_mastermind.chat.prompts.rejection import TITLE_GENERATION_PROMPT
from waldur_mastermind.chat.providers import (
    ALLOWED_COMPLETION_KEYS,
    FALLBACK_DEFAULTS,
    PROVIDER_DEFAULTS,
)
from waldur_mastermind.chat.tools.enums import ToolCategory, ToolName
from waldur_mastermind.chat.tools.executor import ToolExecutor
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.chat.tools.tool_sets import (
    ANONYMOUS_TOOLS,
    get_tool_set_for_user,
)

logger = logging.getLogger(__name__)

# The openai SDK (~6.5 MB) is imported lazily — this module is loaded during
# URLconf resolution at Django startup, but the AI chat is an optional integration
# only exercised when a chat request is served. See the "Lazy imports for heavy
# optional backends" section of CLAUDE.md.
_LLM_ERROR_MESSAGES: dict[type, str] | None = None


def _llm_error_messages() -> dict[type, str]:
    """Map specific OpenAI exception types to user-facing error messages.

    Built lazily (and cached) so importing this module does not pull the openai
    SDK in at startup. Looked up by exact type in the ``except openai.APIError``
    handler; unmatched subclasses fall back to a generic message.
    """
    global _LLM_ERROR_MESSAGES
    if _LLM_ERROR_MESSAGES is None:
        import openai

        _LLM_ERROR_MESSAGES = {
            openai.AuthenticationError: "AI service authentication failed. Please contact your administrator.",
            openai.RateLimitError: "AI service rate limit reached. Please wait and try again.",
            openai.APITimeoutError: "AI service request timed out. Please try again.",
            openai.APIConnectionError: "Could not connect to AI service. Please try again later.",
            openai.InternalServerError: "AI service is temporarily unavailable. Please try again later.",
            openai.NotFoundError: "AI model not found. Please check the configured model name.",
            openai.BadRequestError: "AI service rejected the request. Please try again or contact your administrator.",
        }
    return _LLM_ERROR_MESSAGES


def __getattr__(name):
    # PEP 562: expose ``llm_streamer.openai`` as a lazily-imported attribute so
    # the openai SDK is not pulled in at module import (i.e. at Django startup),
    # while callers and tests that reference the module attribute (e.g.
    # ``mock.patch("...llm_streamer.openai.OpenAI")``) still resolve the real
    # module. Bare ``openai.*`` uses inside functions import it locally instead.
    if name == "openai":
        import openai

        return openai
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Maximum time (seconds) the consumer loop waits for the worker to finish.
_WORKER_TIMEOUT = 300

# Maximum number of NDJSON items buffered between the worker and consumer threads.
_QUEUE_MAXSIZE = 256

# How often (in content chunks) the worker polls the DB for a cross-process
# cancel signal.  A single PK lookup costs ~0.1 ms, so checking every 5
# chunks adds negligible overhead.
_CANCEL_CHECK_INTERVAL = 5

# Maximum number of tool-use rounds per user turn.  A typical chain is
# search → compare (2 rounds); 5 gives headroom without unbounded spend.
# On cap hit the loop forces one final text-only call so the user always
# gets a narrated response.
_MAX_TOOL_ROUNDS = 5


class _StreamDone:
    """Sentinel placed on queue to signal the worker has finished."""


class _StreamError:
    """Placed on queue when the worker encounters an error.

    Carries pre-formatted NDJSON error line(s) for the client.
    """

    def __init__(self, ndjson_lines: list[str]):
        self.ndjson_lines = ndjson_lines


def validate_tool_call(tool_name, user):
    """Validates that the user is authenticated, the tool exists, and the
    tool is in the caller's role-permitted set.

    The per-user tool-set check is the authorisation boundary for the
    HTTP execute endpoint: without it, any authenticated end user could
    invoke staff/support-only tools (e.g. ``get_user_overview``) directly,
    bypassing the LLM-side filter applied via ``get_tool_set_for_user``.
    """
    if not user or not user.is_authenticated:
        raise rf_exceptions.NotAuthenticated()

    if tool_name not in tool_registry:
        raise rf_exceptions.ValidationError(
            {
                "tool": _("Tool '%(tool_name)s' is not recognized.")
                % {"tool_name": tool_name}
            }
        )

    permitted = get_tool_set_for_user(user)
    try:
        tool_enum = ToolName(tool_name)
    except ValueError:
        tool_enum = None
    if tool_enum is None or tool_enum not in permitted:
        raise rf_exceptions.PermissionDenied(
            _("Tool '%(tool_name)s' is not available for your role.")
            % {"tool_name": tool_name}
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
        preload_all_tools=False,
        worker_timeout: int | None = None,
        tool_choice_override: str | None = None,
    ):
        import openai

        self.messages = messages
        self.client = openai.OpenAI(
            api_key=token,
            base_url=url,
            timeout=httpx.Timeout(60.0, connect=5.0),
        )
        self.parser = StreamParser()
        self.accumulated_blocks: list[dict] = []
        # Index into ``accumulated_blocks`` marking the start of the
        # current tool-loop round. ``_extend_with_tool_results`` slices
        # from here so the assistant message it appends to the LLM-facing
        # history contains only this round's text, not all prior rounds.
        self._round_block_offset: int = 0
        self._current_block: dict | None = None
        self.accumulated_warning: str = ""
        self.pending_tool_calls: dict[str, dict] = {}
        self.tool_calls: dict[int, dict] = {}
        self.user = user
        self.input_tokens = None
        self.output_tokens = None
        self.model = ""
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
        self._worker_timeout = (
            worker_timeout if worker_timeout and worker_timeout > 0 else _WORKER_TIMEOUT
        )
        # Per-call tool_choice override, applied AFTER any admin override
        # from AI_ASSISTANT_COMPLETION_KWARGS. Used by the anonymous
        # marketplace path to pin tool_choice="required" — every anon
        # query is about catalog browsing, so a tool call is always the
        # right action and the model's "auto" decisions waste a round on
        # text-only fabrications. Authenticated paths leave this None
        # so admin Constance / "auto" continues to govern.
        self._tool_choice_override = tool_choice_override

        # Thread-based streaming infrastructure
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._client_gone = threading.Event()
        self._cancel_requested = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._chunk_count = 0
        self._persisted = False
        self._flushed = False

        # Per-turn event log. Each step in the agentic loop appends a line;
        # the whole report is flushed as a single logger.info at workflow
        # end so a normal turn produces one consolidated entry instead of
        # 10+ individual log lines.
        self._turn_report: list[str] = []

        # Initial tool surface depends on whether this is the authenticated
        # path (lazy load via search_tools, grow as the LLM asks for tools)
        # or the anonymous path (fixed, narrow surface known up-front).
        if self.user is None:
            # Anonymous chat endpoint: ship the marketplace tools +
            # ask_user up-front. NO search_tools — the anon system prompt
            # doesn't even mention it, and rehydration is meaningless
            # because there's no thread history to walk.
            self._enabled_tool_names = {t.value for t in ANONYMOUS_TOOLS}
        else:
            # Authenticated path. Both meta-tools are seeded so they ship on
            # turn 0 without a search_tools round: ``search_tools`` is the
            # lazy-load mechanism itself, and ``ask_user`` is universal —
            # always available so the LLM can clarify before any data tool.
            self._enabled_tool_names = {
                ToolName.SEARCH_TOOLS.value,
                ToolName.ASK_USER.value,
            }
            self._rehydrate_enabled_tools_from_history()

        if preload_all_tools:
            # Pre-load account tools for validation scenarios
            account_tools = [
                ToolName.DISPLAY_USER_RESOURCES,
                ToolName.LIST_ORGANIZATIONS,
                ToolName.LIST_PROJECTS,
                ToolName.GET_PROJECT_RESOURCES,
                ToolName.GET_PROJECT_QUOTA,
                ToolName.GET_RESOURCE_USAGE,
            ]
            for tool_name in account_tools:
                self._enabled_tool_names.add(tool_name.value)

    def _rehydrate_enabled_tools_from_history(self) -> None:
        """Pre-populate ``_enabled_tool_names`` from prior tool activity.

        Walks the conversation history (replayed from Message.blocks via
        context_assembler) looking for two signals:

        1. Direct assistant tool_calls to any tool — the LLM has a
           working example of that tool's argument shape in its context.
        2. search_tools invocations — the ``categories`` argument tells
           us which buckets were loaded; each expands to its member tools
           via the registry.

        Adding those names to the enabled set skips redundant search_tools
        fetches on every new user turn and eliminates the runtime-guard
        rejection path when the LLM directly calls a previously-used tool.
        """
        scanned = 0
        added_direct: set[str] = set()
        added_via_search: set[str] = set()
        for msg in self.messages or []:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []) or []:
                scanned += 1
                func = tc.get("function") or {}
                name = func.get("name")
                if not name:
                    continue
                if name == ToolName.SEARCH_TOOLS.value:
                    # Parse the categories search_tools loaded in a past
                    # turn and expand each to its member tool names via
                    # the registry.
                    raw_args = func.get("arguments") or "{}"
                    try:
                        args = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str)
                            else raw_args
                        )
                    except (json.JSONDecodeError, TypeError):
                        continue
                    for raw_cat in args.get("categories") or []:
                        if not isinstance(raw_cat, str):
                            continue
                        try:
                            cat = ToolCategory(raw_cat)
                        except ValueError:
                            continue
                        for tool in tool_registry.tools_by_category(cat):
                            fetched_name = tool.definition.name.value
                            if fetched_name not in self._enabled_tool_names:
                                added_via_search.add(fetched_name)
                            self._enabled_tool_names.add(fetched_name)
                else:
                    # Direct call — the tool's schema is visible to the
                    # LLM via the replayed tool_calls entry.
                    if name not in self._enabled_tool_names:
                        added_direct.add(name)
                    self._enabled_tool_names.add(name)
        if scanned or added_direct or added_via_search:
            self._turn_report.append(
                f"rehydrate: scanned {scanned} prior tool_calls; "
                f"direct={sorted(added_direct)} "
                f"via_search_tools={sorted(added_via_search)}"
            )

    def _format_ndjson(self, data: dict) -> str:
        """Helper to format a dict as a Newline Delimited JSON line."""
        return f"{json.dumps(data, separators=(',', ':'))}\n"

    def _stream_completion(self, messages, include_tools=True, round_num: int = 0):
        """Open a streaming chat completion and yield SDK chunk objects."""
        model = config.AI_ASSISTANT_MODEL
        # Captured for persistence: the setting is a mutable global, so the
        # value has to be recorded now, not looked up when the row is saved.
        self.model = model
        backend_type = config.AI_ASSISTANT_BACKEND_TYPE
        _completion_kwargs = config.AI_ASSISTANT_COMPLETION_KWARGS
        completion_kwargs = (
            _completion_kwargs if isinstance(_completion_kwargs, dict) else {}
        )
        user_tools = get_tool_set_for_user(self.user)

        # Lazy tool loading: only expose tools the LLM has actually asked
        # for via search_tools in this turn (plus search_tools itself).
        # ``self._enabled_tool_names`` starts as {"search_tools"} and grows
        # as search_tools calls return. User-permission intersection is
        # enforced: we never offer a tool outside the user's tool_set.
        permitted = {t.value for t in user_tools}
        exposed_names = self._enabled_tool_names & permitted
        exposed_enums = []
        for raw in exposed_names:
            try:
                exposed_enums.append(ToolName(raw))
            except ValueError:
                continue
        tools = tool_registry.get_openai_tools(exposed_enums)

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
            # parallel_tool_calls + tool_choice flow from PROVIDER_DEFAULTS /
            # admin override (AI_ASSISTANT_COMPLETION_KWARGS). Default
            # tool_choice is "auto" — admins can set "required" when the
            # model declines to call tools despite directives. The runtime
            # guard in _execute_tool_calls_worker catches hallucinated
            # calls to unloaded tools and returns an actionable error so
            # the LLM self-corrects.

            # Per-call override (anon path passes "required") wins over
            # the admin Constance setting, which wins over the provider
            # default — BUT only on round 0. Subsequent rounds use
            # whatever was set by Constance (default "auto"). LLM-traffic
            # tracing showed tool_choice="required" applied on every
            # round causes the model to loop on the same tool indefinitely
            # (re-calling with identical args, never narrating), then
            # emit raw <tool_call> markup as text in the forced
            # narration round. Forcing required only on round 0 prevents
            # day-1 fabrication from training-data priors while letting
            # the model narrate naturally once it has tool results.
            if self._tool_choice_override is not None and round_num == 0:
                kwargs["tool_choice"] = self._tool_choice_override
            elif self._tool_choice_override is not None and round_num > 0:
                # Don't let an admin-Constance "required" leak into
                # follow-up rounds either when an override is set on the
                # streamer (anon path semantics).
                kwargs.pop("tool_choice", None)
        else:
            # No tools available — strip tool_choice (vLLM rejects
            # required/named tool_choice without a tools array).
            kwargs.pop("tool_choice", None)

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

        if kind in ("vm_order", "homeport_nav", "resource_list", "ask_user_form"):
            # Tool result chunks arrive here directly now that load chunks
            # are no longer emitted. Persist as top-level blocks so they
            # survive into Message.blocks and render in thread history via
            # the frontend BlockRenderer dispatching on block.key.
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
                "network",
                "ssh_key_name",
                "system_volume_size",
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
        elif kind == "ask_user_form":
            for field in ("questions", "context"):
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
                    if time.monotonic() - start > self._worker_timeout:
                        logger.error(
                            "LLM worker thread exceeded %ds timeout",
                            self._worker_timeout,
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
        import openai

        try:
            self._run_llm_workflow()
        except openai.APIError as e:
            user_msg = _llm_error_messages().get(
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
            # Emit the consolidated turn report as a single log entry.
            # Done first so the trace lands even if downstream persistence
            # or token recording raises.
            if self._turn_report:
                logger.info("LLM turn:\n  %s", "\n  ".join(self._turn_report))
                self._turn_report = []
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
        """Execute the agentic tool-use loop (runs in worker thread).

        The loop exits when the model produces plain text instead of a
        tool_call, when the user is anonymous / stream is cancelled, or when
        the hard round cap is reached — in which case we issue one final
        text-only call so the user always gets a narrated response.
        """
        messages = list(self.messages)

        user_label = "anon" if not self.user else getattr(self.user, "username", "?")
        self._turn_report.insert(
            0,
            f"user={user_label} messages={len(messages)} "
            f"enabled_tools={sorted(self._enabled_tool_names)}",
        )

        for round_num in range(_MAX_TOOL_ROUNDS):
            self.tool_calls = {}
            self._flushed = False

            self._stream_and_collect(messages, round_num=round_num)

            self._turn_report.append(
                f"round {round_num}: tool_calls={len(self.tool_calls)}"
            )
            for tc in self.tool_calls.values():
                self._turn_report.append(f"  → {tc.get('name')}({tc.get('arguments')})")

            if not self.tool_calls:
                fetched = sorted(
                    self._enabled_tool_names - {ToolName.SEARCH_TOOLS.value}
                )
                self._turn_report.append(
                    f"exit: plain text after {round_num + 1} round(s); "
                    f"tools_loaded_this_turn={fetched}"
                )
                return  # LLM produced text — done.
            if self._stopped:
                self._turn_report.append(
                    f"exit: cancelled after {round_num + 1} round(s)"
                )
                return  # Client disconnected or stream cancelled.

            self._execute_tool_calls_worker(self.tool_calls)
            # After execution, enrich the enabled-tools set with anything
            # search_tools just fetched — next round's API call will
            # expose those schemas.
            self._absorb_search_tools_results(self.tool_calls)
            # If any tool just rendered an interactive surface that the
            # user is expected to act on (a form, a preview card, a
            # success/error confirmation), the next move is the user's —
            # not the LLM's. Exit the loop so the model doesn't (a) fire a
            # duplicate ask_user form, or (b) duplicate the rendered data
            # as redundant bullet lists below the card.
            terminal_blocks = {"ask_user_form"}
            terminal_vm_statuses = {"preview", "success", "error"}

            def _is_terminal(entry):
                block = entry.get("_result_block")
                if not isinstance(block, dict):
                    return False
                kind = block.get("k")
                if kind in terminal_blocks:
                    return True
                if kind == "vm_order":
                    status = block.get("status")
                    return status in terminal_vm_statuses
                return False

            if any(_is_terminal(entry) for entry in self.tool_calls.values()):
                self._turn_report.append(
                    f"exit: terminal UI block after {round_num + 1} round(s)"
                )
                return
            messages = self._extend_with_tool_results(messages, self.tool_calls)
            # Mark the start of the next round so its assistant content
            # excludes blocks streamed in earlier rounds.
            self._round_block_offset = len(self.accumulated_blocks)
        else:
            # Hit the cap — force a final text-only call so the user always
            # receives narration rather than a raw tool result block.
            self._turn_report.append(
                f"exit: CAP HIT at {_MAX_TOOL_ROUNDS} rounds — forced text-only call"
            )
            self._flushed = False
            self._stream_and_collect(messages, include_tools=False)

    def _absorb_search_tools_results(self, tool_calls):
        """Grow ``_enabled_tool_names`` from any search_tools calls this round.

        After search_tools runs, the LLM can invoke the fetched tools
        directly on the next round. This method reads the captured result
        data (stashed on each tool_call entry by
        ``_execute_tool_calls_worker``) and adds fetched names to the
        enabled set.
        """
        added: list[str] = []
        missing: list[str] = []
        for entry in tool_calls.values():
            if entry.get("name") != ToolName.SEARCH_TOOLS.value:
                continue
            result_data = entry.get("_result_data") or {}
            fetched = result_data.get("fetched_names") or []
            unknown = result_data.get("missing") or []
            for name in fetched:
                if name not in self._enabled_tool_names:
                    self._enabled_tool_names.add(name)
                    added.append(name)
            missing.extend(unknown)
        if added:
            self._turn_report.append(f"  search_tools loaded: {added}")
        if missing:
            self._turn_report.append(
                f"  search_tools missed (unknown names): {missing}"
            )

    def _extend_with_tool_results(self, messages, tool_calls):
        """Build the next round's message list: original + assistant tool_call + tool results.

        Returns a new list; does not mutate the input.
        """
        followup = list(messages)
        followup.append(
            {
                "role": "assistant",
                "content": blocks_to_text(
                    self.accumulated_blocks[self._round_block_offset :]
                )
                or None,
                "tool_calls": [
                    {
                        "id": entry["id"],
                        "type": "function",
                        "function": {
                            "name": entry["name"],
                            "arguments": entry["arguments"],
                        },
                    }
                    for entry in tool_calls.values()
                ],
            }
        )
        for entry in tool_calls.values():
            # Send the full data dict (not just summary) so the LLM can
            # generate a rich, detailed response from the tool output.
            result_data = entry.get("_result_data", {})
            summary = entry.get("_summary", "Done")
            followup.append(
                {
                    "role": "tool",
                    "tool_call_id": entry["id"],
                    "content": json.dumps(result_data) if result_data else summary,
                }
            )
        return followup

    def _stream_and_collect(self, messages, include_tools=True, round_num: int = 0):
        """Stream one LLM completion, accumulating content and tool calls."""
        with self._stream_completion(
            messages, include_tools=include_tools, round_num=round_num
        ) as stream:
            for chunk in stream:
                if chunk.usage:
                    prompt_tokens = chunk.usage.prompt_tokens or 0
                    completion_tokens = chunk.usage.completion_tokens or 0
                    self.input_tokens = (self.input_tokens or 0) + prompt_tokens
                    self.output_tokens = (self.output_tokens or 0) + completion_tokens

                    details = getattr(chunk.usage, "prompt_tokens_details", None)
                    cached_tokens = getattr(details, "cached_tokens", 0) or 0
                    logger.info(
                        "LLM usage: prompt=%d cached=%d completion=%d",
                        prompt_tokens,
                        cached_tokens,
                        completion_tokens,
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

            # Lazy-load guard: reject tool calls for tools that haven't
            # been fetched via search_tools yet. Tells the LLM exactly how
            # to recover rather than executing with guessed arguments.
            if tool_name not in self._enabled_tool_names:
                self._turn_report.append(
                    f"  ⨯ rejected unloaded tool call: {tool_name}"
                )
                # search_tools takes ``categories``, not ``tool_names`` —
                # look up the unloaded tool's category so the LLM gets a
                # recovery instruction it can actually execute. Fall back
                # to listing all categories when the name is unknown.
                tool = tool_registry.get(tool_name)
                category = tool.definition.category if tool is not None else None
                if category is not None:
                    recovery = f"search_tools(categories=['{category.value}'])"
                else:
                    valid_cats = ", ".join(c.value for c in ToolCategory)
                    recovery = f"search_tools(categories=[<one of: {valid_cats}>])"
                guard_msg = (
                    f"Tool '{tool_name}' is not loaded in this turn. "
                    f"You must call {recovery} FIRST to load its schema, "
                    f"then invoke the tool with the correct arguments in "
                    f"the next round. Do NOT guess the schema."
                )
                entry["_result_block"] = None
                entry["_summary"] = guard_msg
                entry["_result_data"] = {
                    "type": "error",
                    "guard": "lazy_load_required",
                    "tool_name": tool_name,
                    "message": guard_msg,
                }
                if call_id in self.pending_tool_calls:
                    self.pending_tool_calls[call_id]["summary"] = guard_msg
                continue

            # Create the loading tool block in the ACCUMULATOR only — the
            # wire is NOT notified (no skeleton flicker on the frontend).
            # The accumulator uses this loading block as the target into
            # which the subsequent result chunk is wrapped, producing the
            # complete tool block that gets persisted to Message.blocks.
            # Without this, tools whose results return no ui_component
            # never get their tool metadata persisted, and cross-turn
            # rehydration can't see what tools were used in prior turns.
            load_chunk = {"k": "load", "t": "tool", "call_id": call_id}
            self._absorb_block(load_chunk)

            logger.debug(
                "Executing tool call",
                extra={
                    "tool_name": tool_name,
                    # Anonymous path has no user — emit a sentinel so log
                    # parsers that expect the field still get a value.
                    "user_id": self.user.id if self.user else "anon",
                },
            )
            result = tool_executor.execute_tool(tool_name, arguments)
            tool_block = self.parser.parse_tool_result(result)

            # Store result for DB persistence (None for hidden errors)
            entry["_result_block"] = tool_block
            entry["_summary"] = result.get("summary", "")
            # Store full data for LLM follow-up call
            entry["_result_data"] = result.get("data", {})

            result_type = result.get("type", "?")
            self._turn_report.append(
                f"  ← {tool_name} [{result_type}] {result.get('summary', '')[:200]}"
            )

            # Keep accumulator metadata in sync so the tool block has the
            # correct summary when the result chunk is folded in.
            if call_id in self.pending_tool_calls:
                self.pending_tool_calls[call_id]["summary"] = result.get("summary", "")

            if tool_block:
                self._absorb_block({**tool_block, "call_id": call_id})
                self._enqueue(self._format_ndjson(tool_block))
            else:
                # Tools without ui_component (search_tools, list_categories,
                # etc.) still need their tool block finalised so the name/
                # arguments/summary get persisted to Message.blocks — that's
                # what cross-turn rehydration scans to pre-populate
                # _enabled_tool_names. Without this, the loading tool block
                # would be dropped by _finalize_current_block and future
                # turns would re-search for tools they've already used.
                # The synthesised empty markdown chunk becomes the tool's
                # `result` sub-block, which renders as nothing on the frontend.
                self._absorb_block({"k": "markdown", "c": "", "call_id": call_id})

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

        self.assistant_msg.blocks = clean_answer_blocks(self.accumulated_blocks)
        self.assistant_msg.warning = self.accumulated_warning
        self.assistant_msg.input_tokens = self.input_tokens
        self.assistant_msg.output_tokens = self.output_tokens
        self.assistant_msg.model = self.model
        self.assistant_msg.save(
            update_fields=[
                "blocks",
                "warning",
                "input_tokens",
                "output_tokens",
                "model",
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
                blocks=clean_answer_blocks(self.accumulated_blocks),
                warning=self.accumulated_warning,
                sequence_index=user_msg.sequence_index + 1,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                model=self.model,
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

                # Same singleton the anon path increments; mirrors the
                # pre-stream gate in ChatViewSet.stream / anonymous view.
                global_budget = models.GlobalAssistantBudget.get(lock=True)
                global_budget.add_usage(tokens=total_tokens)

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
