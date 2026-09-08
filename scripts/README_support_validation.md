# Support-assistant validation harness

A deterministic validation harness for the authenticated support/staff
assistant. It loads a known demo preset so each scenario has a fixed,
assertable correct answer, then drives the chat assistant as a named user
and scores the response.

## This script or `waldur ai_assistant test_evaluation`?

Both run the same YAML scenarios, as the same user, with the same
evaluators and the same preset.

- **`waldur ai_assistant test_evaluation`** runs *every* pack in one pass
  and exits non-zero on failure — the one to run in CI or before a
  prompt/tool change. A scenario that names a preset is skipped when that
  data is absent, so a run without fixtures reports skips, not failures.
  Add `--preset credit_realistic` to load the data and `--user` to run as
  somebody other than `staff`. Failures print the tool calls and the
  response so they can be diagnosed without a rerun. A run that scores
  nothing — a `--scenario` name that matches no pack, or a run where every
  scenario was skipped — exits non-zero rather than reporting an empty pass.
- **This script** runs *one* pack and writes a Markdown report of every
  prompt, tool call and response, and can dump the LLM wire protocol with
  `--trace-llm`. The one to run when asking "why did the model do that".

## What it does

1. Loads a demo preset (destructive — clears the validation DB).
2. Picks a named user from that preset.
3. Streams each YAML scenario through the in-process LLM streamer with
   that user's identity.
4. Evaluates the response (tool selection, tool arguments, data match,
   pattern checks).
5. Emits a Markdown report.

## One-time setup

Both harnesses run the assistant's tools for real, as the user you pass:
whatever the model decides to call is executed against the connected
database (today only `create_vm` mutates, and one scenario deliberately
tempts it). Loading a preset also wipes existing structure data. So every
run, not only a `--preset` one, belongs on a database of its own — never a
dev or staging one.

```bash
createdb waldur_support_validation
DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
    uv run waldur migrate --run-syncdb
```

The migration takes ~15-25 minutes (one-time) — it's the same set Waldur
runs against `waldur` itself. The same database serves
`test_evaluation --preset`:

```bash
DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
    uv run waldur ai_assistant test_evaluation --preset credit_realistic
```

`--preset` asks for the database name before it deletes anything. For a
non-interactive run, state the name instead: `--wipe-database
waldur_support_validation`. The run stops if that is not the database
`DJANGO_SETTINGS_MODULE` actually selected — which is what catches the
prefix going missing when this command is copied, the one way a harness
run can reach a real database. Once the preset is in, later runs need
neither the flag nor the prompt: the harness detects the data and runs
the packs against it.

Run as `staff` or `support`: account tools return everything to those two
and only role-scoped data to anybody else, so a plain user fails the data
assertions for want of access. The command warns when the user you pass
is neither.

## Run

LLM credentials come from the environment:

```bash
export AI_ASSISTANT_API_URL=https://your-llm-endpoint/v1
export AI_ASSISTANT_API_TOKEN=sk-...
export AI_ASSISTANT_MODEL=qwen3.5-122b-nonthinking
export AI_ASSISTANT_BACKEND_TYPE=vllm   # optional, default 'vllm'

DJANGO_SETTINGS_MODULE=waldur_core.server.support_validation_settings \
    uv run python scripts/support_validation_run.py \
        --preset credit_realistic \
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
shape (`tool_selection.yaml` holds the tool-confusion scenarios: one per
pair of tools whose descriptions overlap, not one per tool):

```yaml
- name: my_scenario
  description: One-line summary.
  inputs:
    - "What the support user types into chat"
  evaluations:
    - type: tool_usage         # right tool selected
      config:
        expected_tool: get_project_resources
        forbidden_tools:       # optional: fail the turn if any of these ran
          - create_vm
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

Name the preset the assertions were written against:

```yaml
- name: my_scenario
  preset: credit_realistic
```

Without it `test_evaluation` runs the scenario against whatever data the
database happens to hold, and a `data_match` miss then looks like an
assistant failure.

Name the scope tier too, when the assertions only hold for one:

```yaml
- name: my_scenario
  scope_tier: end_user
```

The prompt grants different subject matter per tier — `prompts/scope_boundary.py`
lets staff and support answer programming questions, and an end user not — so a
scenario scored under the wrong tier measures the tier rather than the assistant.
`test_evaluation` skips it and names a user of the right tier to rerun with.

## Reusing the migrated DB across runs

Pass `--skip-preset-load` to skip the destructive preset reload — useful
when iterating on scenario YAML against an already-loaded preset. If that
preset is not in fact loaded, the run stops rather than reporting the
missing figures as assistant failures.

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
