"""Universal meta-tool: pose 1–4 structured questions back to the user.

The LLM calls ``ask_user`` when it needs specific user-side detail it
cannot retrieve via another tool — e.g. workload type and budget for a
marketplace recommendation, or a project pick when the upcoming VM-creation
refactor lands. The frontend renders an interactive form (button group for
2–4 options, searchable list for 5–20, free-form text input when options
are absent) and the user's picks come back as a normal user message in
the next turn — same round-trip shape as the existing ``vm_order`` form,
so no streamer pause-and-wait logic is needed.

Pure shape validator: no DB queries, no side effects. Mirrors
``DisplayUserResourcesTool``'s pattern of validating the LLM-supplied
arguments and forwarding a UI signal.
"""

import logging

from waldur_mastermind.chat.tools.base import BaseTool, ToolDefinition
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.registry import tool_registry

logger = logging.getLogger(__name__)

_MIN_QUESTIONS, _MAX_QUESTIONS = 1, 4
_MIN_OPTIONS, _MAX_OPTIONS = 2, 20
_MIN_QUESTION_LEN, _MAX_QUESTION_LEN = 4, 200
_MAX_HEADER_LEN = 40
_MAX_LABEL_LEN = 80
_MAX_DESCRIPTION_LEN = 160
_MAX_VALUE_LEN = 160
_MAX_CONTEXT_LEN = 400

_USAGE_INSTRUCTIONS = (
    "Default channel for any question to the user. Plain-text questions in "
    "your reply are forbidden — including single 'quick' ones. Renders a "
    "form; the user's picks come back as their next message.\n"
    "\n"
    "USE FOR: clarifications, preferences, missing detail, intent gathering "
    "(drafting/planning/recommending), disambiguating ambiguous replies. "
    "Pre-filter long option lists to ≤8 relevant candidates before asking — "
    "never dump raw API results.\n"
    "\n"
    "DO NOT USE FOR: answers retrievable via a tool (use the tool), concept "
    "questions (answer from knowledge), info the user already supplied, "
    "pre-execution 'are you sure?' confirms (just act, or use the right "
    "confirm tool). Never use for VM creation picks — `plan_vm` builds those "
    "forms with the correct server-side filters.\n"
    "\n"
    "RULES:\n"
    "- Max 4 questions per call; each answerable in one tap or one short text entry.\n"
    "- Labels are short noun phrases ('Training LLMs', '$500–$2000'), not sentences.\n"
    "- Every question needs a `header` (1–2 word noun chip) unless the question "
    "itself is already 1–2 words — without it the user's reply parses as garbage.\n"
    "- Don't loop: if a reply is ambiguous, ask a NEW narrower question, never "
    "re-emit the identical form, never clarify in plain text.\n"
    "- After calling ask_user, emit at most one short framing sentence "
    "('A few quick questions:') and STOP."
)


class AskUserTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=ToolName.ASK_USER,
            # Meta-tool: opts out of the category taxonomy. Always available;
            # excluded from the category-grouped catalog and from
            # search_tools fetches.
            category=None,
            description=(
                "Ask the user 1–4 multiple-choice or free-form questions. "
                "Use whenever your reply would otherwise contain a question "
                "to the user — clarifications, picks, intent gathering. "
                "Renders an interactive form; user's picks come back as "
                "their next message. Just call it — don't preview questions "
                "in text. NOT for VM creation picks; use `plan_vm` for "
                "those (it filters options server-side)."
            ),
            # NOTE: schema is intentionally permissive. Some upstream
            # providers (observed: qwen3.5 served via vLLM compat layer)
            # reject schemas that combine ``additionalProperties: false``
            # with deeply nested object items, returning HTTP 400 before
            # the tool can even be called. The runtime ``execute()``
            # already rejects unknown fields and bad shapes, so the
            # belt-and-suspenders strict mode is dropped here.
            inputSchema={
                "type": "object",
                "required": ["questions"],
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": _MIN_QUESTIONS,
                        "maxItems": _MAX_QUESTIONS,
                        "description": f"{_MIN_QUESTIONS}–{_MAX_QUESTIONS} questions.",
                        "items": {
                            "type": "object",
                            "required": ["question"],
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "minLength": _MIN_QUESTION_LEN,
                                    "maxLength": _MAX_QUESTION_LEN,
                                    "description": "Question text, one sentence; don't embed options.",
                                },
                                "header": {
                                    "type": "string",
                                    "maxLength": _MAX_HEADER_LEN,
                                    "description": (
                                        "1–2 word noun chip ('Workload', 'Budget'); "
                                        "required unless question is already 1–2 words."
                                    ),
                                },
                                "options": {
                                    "type": "array",
                                    "minItems": _MIN_OPTIONS,
                                    "maxItems": _MAX_OPTIONS,
                                    "description": (
                                        "2–8 → button group; 9–20 → searchable list. "
                                        "Omit for free-form text. No 'Other' option."
                                    ),
                                    "items": {
                                        "type": "object",
                                        "required": ["label"],
                                        "properties": {
                                            "label": {
                                                "type": "string",
                                                "minLength": 1,
                                                "maxLength": _MAX_LABEL_LEN,
                                                "description": "Short button label.",
                                            },
                                            "description": {
                                                "type": "string",
                                                "maxLength": _MAX_DESCRIPTION_LEN,
                                                "description": "Optional one-line hint under the label.",
                                            },
                                            "value": {
                                                "type": "string",
                                                "maxLength": _MAX_VALUE_LEN,
                                                "description": "Optional machine-friendly id (e.g. UUID).",
                                            },
                                        },
                                    },
                                },
                                "multiSelect": {
                                    "type": "boolean",
                                    "description": "True for multi-pick. Default false.",
                                },
                                "allowFreeText": {
                                    "type": "boolean",
                                    "default": True,
                                    "description": (
                                        "False hides the free-text input; use only for "
                                        "exhaustive/canonical option sets."
                                    ),
                                },
                            },
                        },
                    },
                    "context": {
                        "type": "string",
                        "maxLength": _MAX_CONTEXT_LEN,
                        "description": "Optional one-sentence framing above the first question.",
                    },
                },
            },
            usage_instructions=_USAGE_INSTRUCTIONS,
        )

    def execute(self, user, arguments: dict) -> dict:
        questions = arguments.get("questions")
        if not isinstance(questions, list):
            return self._reject(
                "`questions` must be a list "
                f"of {_MIN_QUESTIONS}–{_MAX_QUESTIONS} question objects."
            )
        if not (_MIN_QUESTIONS <= len(questions) <= _MAX_QUESTIONS):
            return self._reject(
                f"`questions` must contain between {_MIN_QUESTIONS} and "
                f"{_MAX_QUESTIONS} items, got {len(questions)}."
            )

        normalised: list[dict] = []
        for idx, q in enumerate(questions):
            ok, payload = self._normalise_question(idx, q)
            if not ok:
                return self._reject(payload)
            normalised.append(payload)

        ui_data: dict = {"questions": normalised}
        context = arguments.get("context")
        if isinstance(context, str) and context.strip():
            ui_data["context"] = context.strip()[:_MAX_CONTEXT_LEN]

        return {
            "type": "success",
            "summary": (
                f"Asked the user {len(normalised)} question(s). "
                "Wait for their reply before proceeding — do not call "
                "another tool until they respond."
            ),
            "ui_component": "ask_user_form",
            "ui_data": ui_data,
        }

    def _normalise_question(self, idx: int, q: object) -> tuple[bool, object]:
        """Return ``(True, normalised_dict)`` or ``(False, error_message)``.

        Validates the per-question shape and dedupes option labels case-
        insensitively within a single question. Cross-question duplicates
        are intentionally allowed (e.g. two questions with their own
        "None of the above" makes sense).
        """
        if not isinstance(q, dict):
            return False, f"Question #{idx} must be an object."

        question_text = q.get("question")
        if not isinstance(question_text, str):
            return False, f"Question #{idx} is missing a `question` string."
        question_text = question_text.strip()
        if len(question_text) < _MIN_QUESTION_LEN:
            return (
                False,
                f"Question #{idx} text must be at least "
                f"{_MIN_QUESTION_LEN} characters.",
            )
        if len(question_text) > _MAX_QUESTION_LEN:
            return (
                False,
                f"Question #{idx} text must be at most {_MAX_QUESTION_LEN} characters.",
            )

        out: dict = {"id": f"q{idx}", "question": question_text}

        header = q.get("header")
        if header is not None:
            if not isinstance(header, str):
                return False, f"Question #{idx} `header` must be a string."
            header = header.strip()
            if header:
                if len(header) > _MAX_HEADER_LEN:
                    return (
                        False,
                        f"Question #{idx} `header` must be at most "
                        f"{_MAX_HEADER_LEN} characters.",
                    )
                out["header"] = header

        multi_select = q.get("multiSelect", False)
        if not isinstance(multi_select, bool):
            return (
                False,
                f"Question #{idx} `multiSelect` must be a boolean.",
            )
        out["multiSelect"] = multi_select

        allow_free_text = q.get("allowFreeText", True)
        if not isinstance(allow_free_text, bool):
            return (
                False,
                f"Question #{idx} `allowFreeText` must be a boolean.",
            )
        out["allowFreeText"] = allow_free_text

        if "options" in q:
            ok, options_or_err = self._normalise_options(idx, q["options"])
            if not ok:
                return False, options_or_err
            out["options"] = options_or_err

        return True, out

    def _normalise_options(
        self, q_idx: int, raw_options: object
    ) -> tuple[bool, object]:
        if not isinstance(raw_options, list):
            return False, f"Question #{q_idx} `options` must be an array."
        if not (_MIN_OPTIONS <= len(raw_options) <= _MAX_OPTIONS):
            return (
                False,
                f"Question #{q_idx} must have between {_MIN_OPTIONS} and "
                f"{_MAX_OPTIONS} options, got {len(raw_options)}.",
            )

        seen_labels: set[str] = set()
        deduped: list[dict] = []
        for o_idx, opt in enumerate(raw_options):
            if not isinstance(opt, dict):
                return (
                    False,
                    f"Question #{q_idx} option #{o_idx} must be an object "
                    "with at least a `label` field, not a plain string.",
                )
            label = opt.get("label")
            if not isinstance(label, str) or not label.strip():
                return (
                    False,
                    f"Question #{q_idx} option #{o_idx} is missing a `label` string.",
                )
            label = label.strip()
            if len(label) > _MAX_LABEL_LEN:
                return (
                    False,
                    f"Question #{q_idx} option #{o_idx} `label` must be at "
                    f"most {_MAX_LABEL_LEN} characters.",
                )
            key = label.casefold()
            if key in seen_labels:
                # Silent dedupe; rejection only if the survivors fall below
                # the minimum count (handled below).
                continue
            seen_labels.add(key)

            entry: dict = {"id": f"q{q_idx}o{len(deduped)}", "label": label}

            description = opt.get("description")
            if description is not None:
                if not isinstance(description, str):
                    return (
                        False,
                        f"Question #{q_idx} option #{o_idx} `description` "
                        "must be a string.",
                    )
                description = description.strip()
                if description:
                    if len(description) > _MAX_DESCRIPTION_LEN:
                        return (
                            False,
                            f"Question #{q_idx} option #{o_idx} "
                            f"`description` must be at most "
                            f"{_MAX_DESCRIPTION_LEN} characters.",
                        )
                    entry["description"] = description

            value = opt.get("value")
            if value is not None:
                if not isinstance(value, str):
                    return (
                        False,
                        f"Question #{q_idx} option #{o_idx} `value` must be a string.",
                    )
                if len(value) > _MAX_VALUE_LEN:
                    return (
                        False,
                        f"Question #{q_idx} option #{o_idx} `value` must "
                        f"be at most {_MAX_VALUE_LEN} characters.",
                    )
                entry["value"] = value

            deduped.append(entry)

        if len(deduped) < _MIN_OPTIONS:
            return (
                False,
                f"Question #{q_idx} has fewer than {_MIN_OPTIONS} unique "
                "options after deduplication. Provide distinct labels.",
            )

        return True, deduped

    @staticmethod
    def _reject(message: str) -> dict:
        # ``validation_error`` shows the message to the user as markdown
        # AND surfaces it back to the LLM in the next round's tool message
        # (same flow ``DisplayUserResourcesTool`` uses for invalid UUIDs),
        # so the model self-corrects on its next attempt.
        return {
            "type": "validation_error",
            "summary": message,
            "ui_component": "markdown",
            "ui_data": {"c": message},
        }


tool_registry.register(AskUserTool())
