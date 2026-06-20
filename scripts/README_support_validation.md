# Support-assistant validation harness

A deterministic validation harness for the authenticated support/staff
assistant. It loads a known demo preset so each scenario has a fixed,
assertable correct answer, then drives the chat assistant as a named user
and scores the response.

## What it does

1. Loads a demo preset (destructive — clears the validation DB).
2. Picks a named user from that preset.
3. Streams each YAML scenario through the in-process LLM streamer with
   that user's identity.
4. Evaluates the response (tool selection, tool arguments, data match,
   pattern checks).
5. Emits a Markdown report.

## One-time setup

```bash
createdb waldur_support_validation
DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
    uv run waldur migrate --run-syncdb
```

The migration takes ~10-15 minutes (one-time) — it's the same set Waldur
runs against `waldur` itself.

## Run

LLM credentials come from the environment:

```bash
export AI_ASSISTANT_API_URL=https://your-llm-endpoint/v1
export AI_ASSISTANT_API_TOKEN=sk-...
export AI_ASSISTANT_MODEL=qwen3.5-122b-nonthinking
export AI_ASSISTANT_BACKEND_TYPE=vllm   # optional, default 'vllm'

DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
    uv run python scripts/support_validation_run.py \
        --preset credit_management \
        --scenario-file support_credits \
        --user staff
```

Output lands in `support-validation-report.md`. Exit code is non-zero if
any scenario failed.

### Keeping credentials locally

To avoid re-exporting every shell, keep the four values in a local
`ai.env` file (it holds a secret — git-ignore it, never commit it) and
source it before running:

```bash
# ai.env — local only, do NOT commit
export AI_ASSISTANT_API_URL=https://your-llm-endpoint/v1
export AI_ASSISTANT_API_TOKEN=sk-...
export AI_ASSISTANT_MODEL=qwen3.5-122b-nonthinking
export AI_ASSISTANT_BACKEND_TYPE=vllm
```

```bash
source ai.env
DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
    uv run python scripts/support_validation_run.py --scenario-file support_credits
```

## Adding a scenario

Add a new YAML file under
`src/waldur_mastermind/chat/validation_scenarios/support_*.yaml` with this
shape:

```yaml
- name: my_scenario
  description: One-line summary.
  inputs:
    - "What the support user types into chat"
  evaluations:
    - type: tool_usage         # right tool selected
      config:
        expected_tool: get_project_resources
    - type: tool_arguments     # right scope passed
      config:
        tool: get_project_resources
        args_must_contain:
          project_name: Alpha
    - type: data_match         # response cites real ORM values
      config:
        expected_values:
          - "Project Alpha"
          - "17096"
    - type: pattern            # bans hallucinated content
      config:
        forbidden_patterns:
          - "(?i)Project Beta"
```

## Reusing the migrated DB across runs

Pass `--skip-preset-load` to skip the destructive preset reload — useful
when iterating on scenario YAML against an already-loaded preset.

## Wire-protocol LLM trace

For deep diagnosis of "why did the model do X", pass `--trace-llm PATH`
to dump every LLM round (request messages, tool_choice, tool list, full
response content + tool_calls + finish_reason + token usage) to a
Markdown file:

```bash
... scripts/support_validation_run.py \
    --skip-preset-load \
    --trace-llm support-validation-trace.md
```

The trace shows the **full system prompt on round 0** and a tail of the
3 most recent messages on subsequent rounds — enough to see how the
context grows turn by turn without making the file unreadable. Useful
when a scenario fails for a non-obvious reason and you need to see what
the model actually received and emitted at the OpenAI API layer.
