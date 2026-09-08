"""Support-assistant validation harness.

Validation harness for the authenticated support/staff assistant. Loads a
demo preset, picks a named user, streams each scenario through the
in-process Test Client, and emits a Markdown report.

LLM credentials are read from the environment:

    export AI_ASSISTANT_API_URL=https://your-llm-endpoint/v1
    export AI_ASSISTANT_API_TOKEN=sk-...
    export AI_ASSISTANT_MODEL=qwen3.5-122b-nonthinking
    export AI_ASSISTANT_BACKEND_TYPE=vllm   # optional, default 'vllm'

Run::

    DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
        uv run python scripts/support_validation_run.py \
            --preset credit_realistic \
            --scenario-file support_credits \
            --user staff
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

import django

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", "waldur_core.server.support_validation_settings"
)
django.setup()

from constance import config  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.management import call_command  # noqa: E402

from waldur_mastermind.chat.block_schemas import clean_answer_blocks  # noqa: E402
from waldur_mastermind.chat.context_assembler import build_context  # noqa: E402
from waldur_mastermind.chat.llm_streamer import LLMStreamer  # noqa: E402
from waldur_mastermind.chat.validation import turn_blocks  # noqa: E402
from waldur_mastermind.chat.validation.evaluators import get_evaluator  # noqa: E402
from waldur_mastermind.chat.validation.presets import (  # noqa: E402
    is_preset_loaded,
)
from waldur_mastermind.chat.validation.scenarios import (  # noqa: E402
    load_scenarios_from_yaml,
)

# Local helper — wire-protocol LLM trace dumper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _llm_tracer import LLMTracer  # noqa: E402

User = get_user_model()


SCENARIOS_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "waldur_mastermind"
    / "chat"
    / "validation_scenarios"
)


def require_llm_config() -> None:
    """Stop early when the LLM endpoint is not configured.

    support_validation_settings seeds Constance from AI_ASSISTANT_API_URL,
    AI_ASSISTANT_API_TOKEN and AI_ASSISTANT_MODEL in memory; nothing is
    written here, so a token cannot land in a database this script was
    never meant to touch.
    """
    url = (config.AI_ASSISTANT_API_URL or "").strip()
    token = (config.AI_ASSISTANT_API_TOKEN or "").strip()
    model = (config.AI_ASSISTANT_MODEL or "").strip()
    missing = [
        name
        for name, value in (
            ("AI_ASSISTANT_API_URL", url),
            ("AI_ASSISTANT_API_TOKEN", token),
            ("AI_ASSISTANT_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"LLM endpoint not configured: {', '.join(missing)}. Export "
            "AI_ASSISTANT_API_URL, AI_ASSISTANT_API_TOKEN and "
            "AI_ASSISTANT_MODEL and run under "
            "waldur_core.server.support_validation_settings "
            "(AI_ASSISTANT_BACKEND_TYPE is optional, default 'vllm')."
        )
    print(
        f"[llm] backend={config.AI_ASSISTANT_BACKEND_TYPE} model={model} "
        f"url={url} token={token[:6]}...{token[-4:] if len(token) > 10 else ''}"
    )


def load_preset(name: str) -> None:
    print(f"[preset] loading {name} (destructive reset)")
    call_command("demo_presets", "load", name, "--yes")


def pick_user(username: str):
    try:
        return User.objects.get(username=username)
    except User.DoesNotExist as exc:
        usernames = list(User.objects.values_list("username", flat=True))
        raise SystemExit(
            f"user '{username}' not found in preset; available: {usernames}"
        ) from exc


def run_scenario(
    scenario, user, report_lines: list[str], tracer: LLMTracer | None = None
) -> tuple[int, int]:
    """Run a single scenario; return (passed, total) input counts."""
    passed = 0
    total = 0
    report_lines.append("")
    report_lines.append(f"### {scenario.name}")
    report_lines.append("")
    report_lines.append(f"_{scenario.description}_")
    report_lines.append("")

    for input_idx, input_text in enumerate(scenario.inputs):
        total += 1
        start = time.monotonic()
        if tracer is not None:
            tracer.start_session(f"{scenario.name}#{input_idx}")
        try:
            messages = build_context(user=user, user_input=input_text, thread=None)
            streamer = LLMStreamer(
                messages,
                config.AI_ASSISTANT_API_URL,
                config.AI_ASSISTANT_API_TOKEN,
                user=user,
                preload_all_tools=True,
            )
            for _ in streamer:
                pass
            response_text = turn_blocks.response_text(
                clean_answer_blocks(streamer.accumulated_blocks)
            )
            api_tool_calls = turn_blocks.tool_calls_from_blocks(
                streamer.accumulated_blocks
            )
            # Calls the lazy-load guard refused leave no block; they ran
            # nothing, but they say which tool the model reached for.
            rejected_calls = list(streamer.rejected_tool_calls)
        except Exception:  # noqa: BLE001 — surface in report
            report_lines.append("```")
            report_lines.append(traceback.format_exc())
            report_lines.append("```")
            report_lines.append("_💥 ERROR — LLM call raised_")
            continue

        duration_ms = int((time.monotonic() - start) * 1000)

        report_lines.append(f"**Input:** `{input_text}`")
        report_lines.append("")
        report_lines.append("**Tool calls:**")
        report_lines.append("")
        if api_tool_calls:
            for c in api_tool_calls:
                report_lines.append(f"- `{c['name']}({c.get('arguments')})`")
        else:
            report_lines.append("- _(none)_")
        for c in rejected_calls:
            report_lines.append(
                f"- `{c['name']}({c.get('arguments')})` — _refused, not loaded_"
            )
        report_lines.append("")
        report_lines.append("**Response:**")
        report_lines.append("")
        report_lines.append("```")
        report_lines.append(response_text or "(empty)")
        report_lines.append("```")
        report_lines.append("")

        all_passed = True
        for evaluation in scenario.evaluations:
            evaluator = get_evaluator(evaluation.type)
            eval_config = dict(evaluation.config)
            if evaluation.type in {"tool_usage", "tool_arguments"}:
                eval_config["tool_calls"] = api_tool_calls
                eval_config["attempted_tool_calls"] = api_tool_calls + rejected_calls
            elif evaluation.type == "language":
                eval_config["input_text"] = input_text
            result = evaluator.evaluate(response_text, eval_config)
            mark = "✅" if result.passed else "❌"
            report_lines.append(f"- {mark} `{evaluation.type}` — {result.message}")
            if not result.passed:
                all_passed = False
        report_lines.append("")
        report_lines.append(f"_Duration: {duration_ms} ms_")
        report_lines.append("")
        if all_passed:
            passed += 1
        if tracer is not None:
            tracer.flush_session(prompt=input_text)

    return passed, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preset",
        default="credit_realistic",
        help="demo preset to load (default: credit_realistic)",
    )
    parser.add_argument(
        "--scenario-file",
        default="support_credits",
        help="YAML stem under chat/validation_scenarios/ (default: support_credits)",
    )
    parser.add_argument(
        "--user",
        default="staff",
        help="username from the preset to authenticate as (default: staff)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("support-validation-report.md"),
        help="output Markdown report path",
    )
    parser.add_argument(
        "--skip-preset-load",
        action="store_true",
        help="reuse an already-loaded preset (faster iteration)",
    )
    parser.add_argument(
        "--trace-llm",
        type=Path,
        default=None,
        help=(
            "if set, write a wire-protocol LLM trace (every round's "
            "messages, tool_choice, tool_calls, finish_reason) to this "
            "Markdown file"
        ),
    )
    args = parser.parse_args()

    require_llm_config()

    tracer: LLMTracer | None = None
    if args.trace_llm:
        tracer = LLMTracer(args.trace_llm)
        tracer.attach()
        print(f"[trace] writing LLM wire trace to {args.trace_llm}")

    if not args.skip_preset_load:
        load_preset(args.preset)
    elif not is_preset_loaded(args.preset):
        # Every assertion in these packs is a figure from the preset, so
        # running without it produces a report of failures the assistant
        # did not cause.
        raise SystemExit(
            f"preset '{args.preset}' is not loaded in this database. Drop "
            "--skip-preset-load, or point DJANGO_SETTINGS_MODULE at the "
            "database that has it."
        )

    user = pick_user(args.user)
    print(f"[user] running as {user.username} (staff={user.is_staff})")

    scenarios_path = SCENARIOS_DIR / f"{args.scenario_file}.yaml"
    scenarios = load_scenarios_from_yaml(scenarios_path)
    print(f"[scenarios] loaded {len(scenarios)} from {scenarios_path}")

    report_lines: list[str] = []
    report_lines.append(f"# Support-assistant validation — {args.scenario_file}")
    report_lines.append("")
    report_lines.append(
        f"_preset={args.preset} user={user.username} "
        f"model={config.AI_ASSISTANT_MODEL} run_id={uuid.uuid4().hex[:8]}_"
    )

    total_passed = 0
    total_inputs = 0
    for scenario in scenarios:
        p, t = run_scenario(scenario, user, report_lines, tracer=tracer)
        total_passed += p
        total_inputs += t

    report_lines.append("")
    report_lines.append("## Summary")
    report_lines.append("")
    report_lines.append(f"- Inputs run: **{total_inputs}**")
    report_lines.append(f"- Passed: **{total_passed}**")
    report_lines.append(f"- Failed: **{total_inputs - total_passed}**")

    args.report.write_text("\n".join(report_lines) + "\n")
    print(f"[report] {args.report}  ({total_passed}/{total_inputs} passed)")
    return 0 if total_passed == total_inputs else 1


if __name__ == "__main__":
    sys.exit(main())
