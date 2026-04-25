"""Unit tests for tests/select_tests.py — the dynamic test selection logic.

Focus: the .gitlab-ci.yml diff inspection that decides whether a CI-only
change requires a full test run.

Run with:
    pytest tests/test_select_tests.py -v
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from select_tests import (  # noqa: E402
    build_line_to_top_level_key_map,
    collect_touched_top_level_keys,
    parse_diff_hunks,
)


# ---------------------------------------------------------------------------
# build_line_to_top_level_key_map
# ---------------------------------------------------------------------------


def test_line_to_key_map_simple_jobs():
    yaml = textwrap.dedent("""\
        Generate OpenAPI schema:
          stage: deploy
          script:
            - foo
        Build docker image for tests:
          stage: build
          script:
            - bar
        """)
    m = build_line_to_top_level_key_map(yaml)
    assert m[1] == "Generate OpenAPI schema"
    assert m[2] == "Generate OpenAPI schema"
    assert m[4] == "Generate OpenAPI schema"
    assert m[5] == "Build docker image for tests"
    assert m[7] == "Build docker image for tests"


def test_line_to_key_map_anchors():
    yaml = textwrap.dedent("""\
        .unit_test_rules: &unit_test_rules
          rules:
            - if: $CI_COMMIT_BRANCH == "develop"
        Run unit tests:
          <<: *unit_test_rules
        """)
    m = build_line_to_top_level_key_map(yaml)
    assert m[1] == ".unit_test_rules"
    assert m[3] == ".unit_test_rules"
    assert m[4] == "Run unit tests"


def test_line_to_key_map_top_level_lists():
    """Top-level keys whose value is a list (include:, stages:)."""
    yaml = textwrap.dedent("""\
        include:
          - project: foo
            file: bar.yml
        stages:
          - build
          - test
        """)
    m = build_line_to_top_level_key_map(yaml)
    assert m[1] == "include"
    assert m[2] == "include"
    assert m[3] == "include"
    assert m[4] == "stages"
    assert m[6] == "stages"


def test_line_to_key_map_comments_and_blanks():
    yaml = textwrap.dedent("""\
        # Top-level comment
        Generate OpenAPI schema:
          # comment inside job
          stage: deploy

          script:
            - foo
        """)
    m = build_line_to_top_level_key_map(yaml)
    assert m[1] is None  # comment before any key
    assert m[2] == "Generate OpenAPI schema"
    assert m[3] == "Generate OpenAPI schema"
    assert m[5] == "Generate OpenAPI schema"  # blank line


def test_line_to_key_map_ignores_list_items_at_col_0():
    """A list item like '- foo' at column 0 should NOT become a key."""
    yaml = textwrap.dedent("""\
        Job A:
          stage: deploy
        - this_is_weird_but_should_not_become_a_key
        Job B:
          stage: test
        """)
    m = build_line_to_top_level_key_map(yaml)
    assert m[1] == "Job A"
    assert m[3] == "Job A"  # stays attributed to Job A
    assert m[4] == "Job B"


# ---------------------------------------------------------------------------
# parse_diff_hunks
# ---------------------------------------------------------------------------


def test_parse_diff_hunks_basic():
    diff = textwrap.dedent("""\
        diff --git a/.gitlab-ci.yml b/.gitlab-ci.yml
        index 1234567..abcdefg 100644
        --- a/.gitlab-ci.yml
        +++ b/.gitlab-ci.yml
        @@ -10,3 +10,4 @@
        @@ -100,1 +101,2 @@
        """)
    hunks = parse_diff_hunks(diff)
    assert hunks == [(10, 3, 10, 4), (100, 1, 101, 2)]


def test_parse_diff_hunks_default_count():
    """When count is omitted, default to 1."""
    diff = "@@ -42 +42 @@"
    assert parse_diff_hunks(diff) == [(42, 1, 42, 1)]


def test_parse_diff_hunks_zero_count():
    """When count is 0, no lines were added/removed at that location."""
    diff = "@@ -10,0 +11,3 @@"
    assert parse_diff_hunks(diff) == [(10, 0, 11, 3)]


# ---------------------------------------------------------------------------
# collect_touched_top_level_keys (integration)
# ---------------------------------------------------------------------------


BASE_YAML = textwrap.dedent("""\
    Generate OpenAPI schema:
      stage: deploy
      script:
        - echo schema
    Build docker image for tests:
      stage: build
      script:
        - buildah build
    Run unit tests:
      stage: test
      script:
        - pytest
    """)


def test_collect_touched_keys_only_deploy():
    head_yaml = textwrap.dedent("""\
        Generate OpenAPI schema:
          stage: deploy
          script:
            - echo schema-v2
        Build docker image for tests:
          stage: build
          script:
            - buildah build
        Run unit tests:
          stage: test
          script:
            - pytest
        """)
    diff = textwrap.dedent("""\
        @@ -4,1 +4,1 @@
        -    - echo schema
        +    - echo schema-v2
        """)
    touched = collect_touched_top_level_keys(diff, BASE_YAML, head_yaml)
    assert touched == {"Generate OpenAPI schema"}


def test_collect_touched_keys_test_job():
    head_yaml = textwrap.dedent("""\
        Generate OpenAPI schema:
          stage: deploy
          script:
            - echo schema
        Build docker image for tests:
          stage: build
          script:
            - buildah build
        Run unit tests:
          stage: test
          script:
            - pytest -x
        """)
    diff = textwrap.dedent("""\
        @@ -12,1 +12,1 @@
        -    - pytest
        +    - pytest -x
        """)
    touched = collect_touched_top_level_keys(diff, BASE_YAML, head_yaml)
    assert touched == {"Run unit tests"}


def test_collect_touched_keys_multiple_jobs():
    head_yaml = textwrap.dedent("""\
        Generate OpenAPI schema:
          stage: deploy
          script:
            - echo schema-v2
        Build docker image for tests:
          stage: build
          script:
            - buildah build --new-flag
        Run unit tests:
          stage: test
          script:
            - pytest
        """)
    diff = textwrap.dedent("""\
        @@ -4,1 +4,1 @@
        -    - echo schema
        +    - echo schema-v2
        @@ -8,1 +8,1 @@
        -    - buildah build
        +    - buildah build --new-flag
        """)
    touched = collect_touched_top_level_keys(diff, BASE_YAML, head_yaml)
    assert touched == {"Generate OpenAPI schema", "Build docker image for tests"}


# ---------------------------------------------------------------------------
# Decision logic via mocked ci_diff_affects_tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_diff_inputs(monkeypatch):
    """Replace subprocess.check_output to return preset diff/yaml content."""
    state: dict = {}

    def fake_check_output(cmd, text=True):
        if cmd[:2] == ["git", "diff"]:
            return state["diff"]
        if cmd[:2] == ["git", "show"]:
            arg = cmd[2]
            if arg.endswith(":.gitlab-ci.yml"):
                sha = arg.split(":", 1)[0]
                return state["base"] if sha == "BASE" else state["head"]
        raise AssertionError(f"unexpected command: {cmd}")

    import select_tests

    monkeypatch.setattr(select_tests.subprocess, "check_output", fake_check_output)
    return state


def test_full_run_when_test_stage_job_changes(fake_diff_inputs):
    fake_diff_inputs["base"] = BASE_YAML
    fake_diff_inputs["head"] = BASE_YAML.replace("- pytest", "- pytest -x")
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -12,1 +12,1 @@
        -    - pytest
        +    - pytest -x
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is True


def test_no_full_run_for_deploy_only_change(fake_diff_inputs):
    fake_diff_inputs["base"] = BASE_YAML
    fake_diff_inputs["head"] = BASE_YAML.replace("- echo schema", "- echo schema-v2")
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -4,1 +4,1 @@
        -    - echo schema
        +    - echo schema-v2
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is False


def test_full_run_when_unit_test_rules_change(fake_diff_inputs):
    base = textwrap.dedent("""\
        .unit_test_rules: &unit_test_rules
          rules:
            - if: $SKIP_TESTS == "true"
              when: never
            - if: $CI_COMMIT_BRANCH == "develop"
        Generate OpenAPI schema:
          stage: deploy
          script:
            - echo schema
        """)
    head = textwrap.dedent("""\
        .unit_test_rules: &unit_test_rules
          rules:
            - if: $SKIP_TESTS == "true"
              when: never
            - if: $CI_COMMIT_BRANCH == "main"
        Generate OpenAPI schema:
          stage: deploy
          script:
            - echo schema
        """)
    fake_diff_inputs["base"] = base
    fake_diff_inputs["head"] = head
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -5,1 +5,1 @@
        -    - if: $CI_COMMIT_BRANCH == "develop"
        +    - if: $CI_COMMIT_BRANCH == "main"
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is True


def test_full_run_when_global_include_changes(fake_diff_inputs):
    base = textwrap.dedent("""\
        include:
          - project: a
            file: b.yml
        Generate OpenAPI schema:
          stage: deploy
        """)
    head = textwrap.dedent("""\
        include:
          - project: a
            file: c.yml
        Generate OpenAPI schema:
          stage: deploy
        """)
    fake_diff_inputs["base"] = base
    fake_diff_inputs["head"] = head
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -3,1 +3,1 @@
        -    file: b.yml
        +    file: c.yml
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is True


def test_no_full_run_when_diff_is_empty(fake_diff_inputs):
    fake_diff_inputs["base"] = BASE_YAML
    fake_diff_inputs["head"] = BASE_YAML
    fake_diff_inputs["diff"] = ""
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is False
