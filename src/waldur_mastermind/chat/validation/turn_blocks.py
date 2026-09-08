"""What the evaluators read out of a finished turn's blocks.

Shared by ``waldur ai_assistant test_evaluation`` and
``scripts/support_validation_run.py`` so both harnesses score the same
thing.
"""

from waldur_mastermind.chat.block_schemas import blocks_to_text


def tool_calls_from_blocks(blocks: list[dict]) -> list[dict]:
    """Every tool call of the turn, in order, as the evaluators expect it.

    ``LLMStreamer.tool_calls`` is reset each round of the agentic loop, so
    after the stream ends it only holds the last round — usually the empty
    text round. The persisted tool blocks are the cumulative record.
    """
    return [
        {
            "name": block["tool"].get("name"),
            "arguments": block["tool"].get("arguments") or {},
        }
        for block in blocks
        if block.get("key") == "tool" and block.get("tool", {}).get("name")
    ]


def ask_user_text(blocks: list[dict]) -> str:
    """Question and context text of any ``ask_user`` form in the turn.

    An ask_user form is a terminal block: the loop exits and the model
    never narrates, so the form *is* the answer. ``blocks_to_text`` skips
    it on purpose (it also feeds search_text and the LLM-facing history),
    hence the harness-local extraction. The form normally sits inside the
    tool block's ``result``; a provider that omits ``call_id`` leaves it
    as a top-level block instead, so both shapes are read.
    """
    parts: list[str] = []
    for block in blocks:
        form = block.get("result") if block.get("key") == "tool" else block
        if not isinstance(form, dict) or form.get("key") != "ask_user_form":
            continue
        if form.get("context"):
            parts.append(form["context"])
        for question in form.get("questions", []):
            if not question:
                continue
            parts.append(question.get("question", ""))
            # The options are shown too, and often carry the answer
            # ("Which area?" with one category as the only choice).
            for option in question.get("options") or []:
                parts.append(option.get("label", ""))
                parts.append(option.get("description", ""))
    return "\n".join(p for p in parts if p)


def response_text(blocks: list[dict]) -> str:
    """The turn's answer as the evaluators should see it.

    Text is taken raw: the prompt_following pack forbids ``tool`` /
    ``function`` leaking into the answer, which is exactly what
    ``clean_answer_blocks`` would strip. Callers that want the user-facing
    view clean the blocks first.
    """
    narration = blocks_to_text(blocks).strip()
    return "\n".join(part for part in (narration, ask_user_text(blocks)) if part)
