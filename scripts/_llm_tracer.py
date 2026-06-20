"""LLM-traffic tracer — wire-protocol-level capture for the harness.

Wraps ``openai.OpenAI.chat.completions.create`` so every LLM round
records its full request (system prompt, message list, tool specs,
tool_choice) and full response (content chunks, tool-call deltas,
finish_reason, usage). Output is rendered as Markdown.

Usage::

    from _llm_tracer import LLMTracer
    tracer = LLMTracer(report_path)
    tracer.attach()                       # monkeypatch LLMStreamer
    tracer.start_session("scenario_x")    # reset state per scenario
    # … run a scenario through the harness …
    tracer.flush_session(prompt=user_input)  # emit Markdown for this run

The support-assistant harness opts in via ``--trace-llm PATH``.
"""

from __future__ import annotations

import datetime
from pathlib import Path


class _StreamProxy:
    """Wrap an OpenAI streaming iterator and accumulate the response."""

    def __init__(self, stream, tracer: LLMTracer, round_num: int):
        self._stream = stream
        self._tracer = tracer
        self._round = round_num
        self._content_chunks: list[str] = []
        self._tool_call_chunks: dict[int, dict] = {}
        self._finish_reason: str | None = None
        self._usage: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._emit_response_event()
        if hasattr(self._stream, "__exit__"):
            return self._stream.__exit__(*exc)

    def _emit_response_event(self) -> None:
        consolidated_tools = []
        for idx, tc in sorted(self._tool_call_chunks.items()):
            consolidated_tools.append(
                {
                    "id": tc.get("id", ""),
                    "name": (tc.get("function") or {}).get("name", ""),
                    "arguments": (tc.get("function") or {}).get("arguments", ""),
                }
            )
        self._tracer.events.append(
            {
                "type": "response",
                "session": self._tracer.session_label,
                "round": self._round,
                "finish_reason": self._finish_reason,
                "content": "".join(self._content_chunks),
                "tool_calls": consolidated_tools,
                "usage": self._usage,
            }
        )
        self._tracer.round_num += 1

    def __iter__(self):
        for chunk in self._stream:
            usage = getattr(chunk, "usage", None)
            if usage:
                self._usage = {
                    "prompt": usage.prompt_tokens or 0,
                    "completion": usage.completion_tokens or 0,
                }
            choices = getattr(chunk, "choices", None)
            if not choices:
                yield chunk
                continue
            choice = choices[0]
            delta = choice.delta
            if getattr(delta, "content", None):
                self._content_chunks.append(delta.content)
            if getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    idx = tc.index
                    slot = self._tool_call_chunks.setdefault(idx, {"function": {}})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["function"]["name"] = (
                                slot["function"].get("name", "") + tc.function.name
                            )
                        if tc.function.arguments:
                            slot["function"]["arguments"] = (
                                slot["function"].get("arguments", "")
                                + tc.function.arguments
                            )
            if getattr(choice, "finish_reason", None):
                self._finish_reason = choice.finish_reason
            yield chunk


def _summarise_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, str):
            preview = content[:600] + ("…" if len(content) > 600 else "")
        else:
            preview = repr(content)[:200]
        entry = {"role": role, "content": preview}
        if "tool_calls" in m and m["tool_calls"]:
            entry["tool_calls"] = [
                {
                    "name": (tc.get("function") or {}).get("name"),
                    "args": (tc.get("function") or {}).get("arguments", "")[:200],
                }
                for tc in m["tool_calls"]
            ]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        out.append(entry)
    return out


class LLMTracer:
    """Per-run trace recorder. One instance per harness invocation."""

    def __init__(self, report_path: Path):
        self.report_path = Path(report_path)
        self.events: list[dict] = []
        self.session_label = "(none)"
        self.round_num = 0
        self._attached = False

    # ---- attachment -------------------------------------------------------
    def attach(self) -> None:
        """Monkeypatch LLMStreamer so every constructed streamer is traced."""
        if self._attached:
            return
        from waldur_mastermind.chat import llm_streamer

        orig_init = llm_streamer.LLMStreamer.__init__
        tracer = self

        def _traced_init(self, *args, **kwargs):
            orig_init(self, *args, **kwargs)
            tracer._wrap_client(self.client)

        llm_streamer.LLMStreamer.__init__ = _traced_init

        # initialise file with header
        self.report_path.write_text("# Support-assistant LLM trace\n\n")
        self._emit(
            f"_Run started {datetime.datetime.utcnow().isoformat()}_\n\n"
            "Each scenario shows every LLM round: the request "
            "(messages, tool_choice, tools available) and the response "
            "(content, tool_calls, finish_reason, usage).\n"
        )
        self._attached = True

    def _wrap_client(self, client) -> None:
        original = client.chat.completions.create
        tracer = self

        def wrapped(**kwargs):
            messages = kwargs.get("messages", [])
            tools = kwargs.get("tools") or []
            tool_choice = kwargs.get("tool_choice", "<unset>")
            tracer.events.append(
                {
                    "type": "request",
                    "session": tracer.session_label,
                    "round": tracer.round_num,
                    "model": kwargs.get("model"),
                    "tool_choice": tool_choice,
                    "tools_count": len(tools),
                    "tool_names": [
                        (t.get("function") or {}).get("name") for t in tools
                    ],
                    "messages_count": len(messages),
                    "messages": _summarise_messages(messages),
                }
            )
            stream = original(**kwargs)
            return _StreamProxy(stream, tracer, tracer.round_num)

        client.chat.completions.create = wrapped

    # ---- per-scenario lifecycle ------------------------------------------
    def start_session(self, label: str) -> None:
        self.session_label = label
        self.events = []
        self.round_num = 0

    def flush_session(self, *, prompt: str) -> None:
        self._emit(f"\n## `{self.session_label}`\n")
        self._emit(f"**Prompt:** {prompt}\n")
        n_rounds = sum(1 for e in self.events if e["type"] == "request")
        self._emit(f"**LLM rounds:** {n_rounds}\n")
        for ev in self.events:
            if ev["type"] == "request":
                self._emit_request(ev)
            else:
                self._emit_response(ev)

    # ---- writers ----------------------------------------------------------
    def _emit(self, text: str) -> None:
        with open(self.report_path, "a") as f:
            f.write(text.rstrip() + "\n")

    def _emit_request(self, ev: dict) -> None:
        self._emit(f"\n### Round {ev['round']} — request\n")
        self._emit(f"- model: `{ev['model']}`")
        self._emit(f"- tool_choice: `{ev['tool_choice']}`")
        self._emit(f"- tools available ({ev['tools_count']}): {ev['tool_names']}")
        self._emit(f"- messages: {ev['messages_count']}")
        self._emit("")

        msgs = ev["messages"]
        if not msgs:
            return
        # Only on round 0 do we dump the full system prompt.
        show_system_full = ev["round"] == 0 and msgs[0]["role"] == "system"
        if show_system_full:
            self._emit("**System prompt (full):**\n")
            self._emit("```")
            self._emit(msgs[0]["content"])
            self._emit("```\n")
            tail = msgs[1:][-3:]
        else:
            tail = msgs[-3:]
        for m in tail:
            self._emit(_format_message(m))
            self._emit("")

    def _emit_response(self, ev: dict) -> None:
        self._emit(f"### Round {ev['round']} — response\n")
        self._emit(f"- finish_reason: `{ev['finish_reason']}`")
        if ev["usage"]:
            self._emit(
                f"- usage: prompt={ev['usage']['prompt']} "
                f"completion={ev['usage']['completion']}"
            )
        self._emit("")
        if ev["content"]:
            self._emit("**Content:**\n")
            self._emit("```")
            content = ev["content"]
            self._emit(content[:1500] + ("…" if len(content) > 1500 else ""))
            self._emit("```")
        if ev["tool_calls"]:
            self._emit("**Tool calls emitted:**")
            for tc in ev["tool_calls"]:
                args = tc["arguments"][:300]
                self._emit(f"  - `{tc['name']}({args})`")
        if not ev["content"] and not ev["tool_calls"]:
            self._emit("_(empty response)_")
        self._emit("")


def _format_message(m: dict) -> str:
    role = m["role"].upper()
    content = m["content"]
    if role == "SYSTEM":
        # already shown in full at round 0; truncate for non-round-0 reuse
        content = content[:200] + " […truncated…]" if len(content) > 200 else content
    pieces = [f"**{role}:**", "```", content, "```"]
    if m.get("tool_calls"):
        pieces.append("_tool_calls:_")
        for tc in m["tool_calls"]:
            pieces.append(f"  - `{tc['name']}({tc['args']})`")
    if m.get("tool_call_id"):
        pieces.append(f"_in reply to tool_call_id: `{m['tool_call_id']}`_")
    return "\n".join(pieces)
