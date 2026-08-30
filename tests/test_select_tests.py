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
import yaml

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


# ---------------------------------------------------------------------------
# Stage resolution through `extends:` and image-job exclusion (#293)
# ---------------------------------------------------------------------------

EXTENDS_YAML = textwrap.dedent("""\
    .Unit test runner:
      stage: test
      script:
        - pytest
    Run migration tests:
      extends: .Unit test runner
      script:
        - waldur migrate
    Test Multi-arch docker image build:
      stage: test
      script:
        - buildah build
    Upload configuration guide:
      stage: postdeploy
      script:
        - echo upload
    """)


def test_resolve_stage_follows_extends():
    """A job inheriting its stage must not read as stage-less."""
    from select_tests import GitLabSafeLoader, resolve_stage

    config = yaml.load(EXTENDS_YAML, Loader=GitLabSafeLoader)
    assert resolve_stage("Test Multi-arch docker image build", config) == "test"
    # The regression: this returned None, so the job was reported as a
    # deploy/postdeploy/release job and skipped the full run.
    assert resolve_stage("Run migration tests", config) == "test"
    assert resolve_stage("Upload configuration guide", config) == "postdeploy"
    assert resolve_stage("No such job", config) is None


def test_resolve_stage_survives_extends_cycle():
    """A malformed config must not hang or recurse forever."""
    from select_tests import GitLabSafeLoader, resolve_stage

    config = yaml.load(
        textwrap.dedent("""\
            A:
              extends: B
            B:
              extends: A
            """),
        Loader=GitLabSafeLoader,
    )
    assert resolve_stage("A", config) is None


def test_full_run_when_job_inherits_test_stage(fake_diff_inputs):
    """Editing a test job that gets its stage via `extends:` must run tests."""
    fake_diff_inputs["base"] = EXTENDS_YAML
    fake_diff_inputs["head"] = EXTENDS_YAML.replace(
        "- waldur migrate", "- waldur migrate --noinput"
    )
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -7,1 +7,1 @@
        -    - waldur migrate
        +    - waldur migrate --noinput
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is True


def test_no_full_run_for_image_build_job(fake_diff_inputs):
    """A buildah job sits in `test` but cannot affect the Python suite."""
    fake_diff_inputs["base"] = EXTENDS_YAML
    fake_diff_inputs["head"] = EXTENDS_YAML.replace(
        "- buildah build", "- buildah build --platform=linux/amd64"
    )
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -11,1 +11,1 @@
        -    - buildah build
        +    - buildah build --platform=linux/amd64
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is False


# ---------------------------------------------------------------------------
# Second-level attribution: rules-only diffs (#293)
# ---------------------------------------------------------------------------

RULES_YAML = textwrap.dedent("""\
    Generate OpenAPI schema:
      extends: .Unit test runner
      stage: test
      rules:
        - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      script:
        - waldur spectacular
    """)


def test_changed_job_keys_sees_through_merge_anchors():
    """Swapping which rules anchor a job merges reads as a `rules` change.

    The diff line says `<<: *other`, but PyYAML resolves merge keys, so the
    comparison sees `rules` and nothing else. Line attribution reported `<<`
    and so missed the exemption.
    """
    from select_tests import GitLabSafeLoader, changed_job_keys

    def load(anchor_rules):
        return yaml.load(
            textwrap.dedent(f"""\
                .a: &a
                  rules: [{{if: A}}]
                .b: &b
                  rules: [{{if: B}}]
                Job:
                  <<: *{anchor_rules}
                  stage: test
                  script: [pytest]
                """),
            Loader=GitLabSafeLoader,
        )

    assert changed_job_keys("Job", load("a"), load("b")) == {"rules"}
    assert changed_job_keys("Job", load("a"), load("a")) == set()
    assert changed_job_keys("Nope", load("a"), load("a")) is None


def test_changed_job_keys_ignores_comments():
    """A comment changes no key, so the job compares as unchanged."""
    from select_tests import GitLabSafeLoader, changed_job_keys

    base = yaml.load(
        "Job:\n  stage: test\n  script: [pytest]\n", Loader=GitLabSafeLoader
    )
    head = yaml.load(
        "Job:\n  # explanatory comment\n  stage: test\n  script: [pytest]\n",
        Loader=GitLabSafeLoader,
    )
    assert changed_job_keys("Job", base, head) == set()


def test_no_full_run_when_only_rules_change(fake_diff_inputs):
    """The !6135 case: gating a test job's rules must not run the suite."""
    fake_diff_inputs["base"] = RULES_YAML
    fake_diff_inputs["head"] = RULES_YAML.replace(
        '    - if: $CI_PIPELINE_SOURCE == "merge_request_event"\n',
        '    - if: $CI_PIPELINE_SOURCE == "merge_request_event"\n'
        "      changes:\n        - src/**/*\n",
    )
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -5,0 +6,2 @@
        +      changes:
        +        - src/**/*
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is False


def test_full_run_when_script_changes_alongside_rules(fake_diff_inputs):
    """A script change in the same job still forces a full run."""
    head = RULES_YAML.replace("- waldur spectacular", "- waldur spectacular --validate")
    fake_diff_inputs["base"] = RULES_YAML
    fake_diff_inputs["head"] = head
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -5,1 +5,1 @@
        -    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
        +    - if: $CI_PIPELINE_SOURCE == "schedule"
        @@ -7,1 +7,1 @@
        -    - waldur spectacular
        +    - waldur spectacular --validate
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is True


def test_no_full_run_when_new_anchor_added_after_test_infrastructure(fake_diff_inputs):
    """The !6137 case.

    Adding a documented anchor immediately after `.unit_test_rules` blamed the
    new comment block on `.unit_test_rules` — test infrastructure — and forced a
    full run, even though that key was untouched and the jobs below only swapped
    which rules anchor they merge.
    """
    base = textwrap.dedent("""\
        .unit_test_rules: &unit_test_rules
          rules:
            - if: $CI_PIPELINE_SOURCE == "merge_request_event"
              changes: [src/**/*, .gitlab-ci.yml]
        Check Action decorators:
          stage: test
          <<: *unit_test_rules
          script: [check]
        """)
    head = textwrap.dedent("""\
        .unit_test_rules: &unit_test_rules
          rules:
            - if: $CI_PIPELINE_SOURCE == "merge_request_event"
              changes: [src/**/*, .gitlab-ci.yml]

        # Documentation for the new anchor. These comment lines sit between the
        # two keys and were previously attributed to .unit_test_rules.
        .source_analysis_rules: &source_analysis_rules
          rules:
            - if: $CI_PIPELINE_SOURCE == "merge_request_event"
              changes: [src/**/*]
        Check Action decorators:
          stage: test
          <<: *source_analysis_rules
          script: [check]
        """)
    fake_diff_inputs["base"] = base
    fake_diff_inputs["head"] = head
    fake_diff_inputs["diff"] = textwrap.dedent("""\
        @@ -4,0 +5,8 @@
        +
        +# Documentation for the new anchor. These comment lines sit between the
        +# two keys and were previously attributed to .unit_test_rules.
        +.source_analysis_rules: &source_analysis_rules
        +  rules:
        +    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
        +      changes: [src/**/*]
        @@ -7,1 +15,1 @@
        -  <<: *unit_test_rules
        +  <<: *source_analysis_rules
        """)
    from select_tests import ci_diff_affects_tests

    assert ci_diff_affects_tests("BASE", "HEAD") is False


# ---------------------------------------------------------------------------
# Path classification and selection (select_paths)
# ---------------------------------------------------------------------------

from select_tests import (  # noqa: E402
    is_local_only,
    is_test_irrelevant,
    pyproject_affects_tests,
    select_paths,
)

GRAPH = {
    "waldur_core.core": set(),
    "waldur_core.structure": {"waldur_core.core"},
    "waldur_mastermind.marketplace": {"waldur_core.core", "waldur_core.structure"},
    "waldur_mastermind.proposal": {"waldur_mastermind.marketplace"},
    "waldur_mastermind.chat": set(),
}


def _apps(paths):
    return {p.removeprefix("src/").replace("/", ".") for p in paths}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/waldur_core/core/migrations/0046_x.py", True),
        ("src/waldur_core/core/locale/et/LC_MESSAGES/django.po", True),
        ("docs/guides/foo.md", True),
        ("src/waldur_core/core/models.py", False),
        ("src/waldur_core/core/tests/test_x.py", False),
    ],
)
def test_is_test_irrelevant(path, expected):
    assert is_test_irrelevant(path) is expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/waldur_mastermind/marketplace/tests/test_x.py", True),
        ("src/waldur_mastermind/marketplace/templates/marketplace/a.html", True),
        ("src/waldur_mastermind/marketplace/static/x.js", True),
        ("src/waldur_mastermind/marketplace/management/commands/x.py", False),
        ("src/waldur_mastermind/marketplace/serializers.py", False),
    ],
)
def test_is_local_only(path, expected):
    assert is_local_only(path) is expected


def test_shared_change_fans_out_to_direct_dependents():
    paths = select_paths(["src/waldur_core/structure/models.py"], GRAPH, None)
    assert _apps(paths) == {"waldur_core.structure", "waldur_mastermind.marketplace"}


def test_test_only_change_does_not_fan_out():
    paths = select_paths(
        ["src/waldur_mastermind/marketplace/tests/test_orders.py"], GRAPH, None
    )
    assert _apps(paths) == {"waldur_mastermind.marketplace"}


def test_migration_is_ignored_but_its_test_selects_the_app():
    paths = select_paths(
        [
            "src/waldur_core/core/migrations/0046_seed.py",
            "src/waldur_core/core/tests/test_seed.py",
        ],
        GRAPH,
        None,
    )
    assert _apps(paths) == {"waldur_core.core"}


def test_migration_alone_selects_nothing():
    assert (
        select_paths(["src/waldur_core/core/migrations/0046_seed.py"], GRAPH, None)
        == []
    )


def test_mixed_change_fans_out_only_from_shared_files():
    paths = select_paths(
        [
            "src/waldur_core/core/tests/test_x.py",  # local-only: no fan-out
            "src/waldur_mastermind/marketplace/serializers.py",  # shared: fan-out
        ],
        GRAPH,
        None,
    )
    assert _apps(paths) == {
        "waldur_core.core",
        "waldur_mastermind.marketplace",
        "waldur_mastermind.proposal",
    }


def test_new_app_not_in_graph_is_not_silently_dropped():
    # The graph always lists every app (with an empty set), so a file in a
    # newly added app maps to it.
    paths = select_paths(["src/waldur_mastermind/chat/views.py"], GRAPH, None)
    assert _apps(paths) == {"waldur_mastermind.chat"}


def test_permissions_is_no_longer_a_full_run_trigger():
    graph = dict(GRAPH, **{"waldur_core.permissions": set()})
    graph["waldur_core.structure"] = {"waldur_core.permissions"}
    paths = select_paths(["src/waldur_core/permissions/serializers.py"], graph, None)
    assert paths != ["src"]
    assert _apps(paths) == {"waldur_core.permissions", "waldur_core.structure"}


def test_full_run_triggers_still_win():
    assert select_paths(["uv.lock"], GRAPH, None) == ["src"]
    assert select_paths(["src/waldur_core/server/base_settings.py"], GRAPH, None) == [
        "src"
    ]
    assert select_paths(["conftest.py"], GRAPH, None) == ["src"]


def test_selector_change_runs_selector_tests_not_suite():
    assert select_paths(["tests/select_tests.py"], GRAPH, None) == [
        "tests/test_select_tests.py"
    ]
    assert select_paths(["tests/build_dependency_graph.py"], GRAPH, None) == [
        "tests/test_select_tests.py"
    ]


def test_pyproject_without_endpoints_is_a_full_run():
    assert select_paths(["pyproject.toml"], GRAPH, None) == ["src"]


BASE_PYPROJECT = textwrap.dedent("""\
    [project]
    name = "waldur-mastermind"
    version = "7.0.0"
    dependencies = ["django==5.1"]

    [dependency-groups]
    dev = ["pytest"]

    [tool.ruff]
    line-length = 88
    """)


def test_pyproject_version_bump_does_not_affect_tests():
    head = BASE_PYPROJECT.replace('version = "7.0.0"', 'version = "7.1.0"')
    assert pyproject_affects_tests(BASE_PYPROJECT, head) is False


def test_pyproject_ruff_config_does_not_affect_tests():
    head = BASE_PYPROJECT.replace("line-length = 88", "line-length = 100")
    assert pyproject_affects_tests(BASE_PYPROJECT, head) is False


def test_pyproject_dependency_change_affects_tests():
    head = BASE_PYPROJECT.replace("django==5.1", "django==5.2")
    assert pyproject_affects_tests(BASE_PYPROJECT, head) is True


def test_pyproject_dev_group_change_affects_tests():
    head = BASE_PYPROJECT.replace('dev = ["pytest"]', 'dev = ["pytest", "freezegun"]')
    assert pyproject_affects_tests(BASE_PYPROJECT, head) is True


def test_pyproject_unparsable_or_missing_affects_tests():
    assert pyproject_affects_tests(BASE_PYPROJECT, "[project\n") is True
    assert pyproject_affects_tests(None, BASE_PYPROJECT) is True


def test_pyproject_version_bump_selects_only_changed_apps(monkeypatch):
    import select_tests

    head = BASE_PYPROJECT.replace('version = "7.0.0"', 'version = "7.1.0"')
    monkeypatch.setattr(
        select_tests,
        "_git_show",
        lambda sha, path: BASE_PYPROJECT if sha == "BASE" else head,
    )
    paths = select_paths(
        ["pyproject.toml", "src/waldur_mastermind/chat/views.py"],
        GRAPH,
        ("BASE", "HEAD"),
    )
    assert _apps(paths) == {"waldur_mastermind.chat"}


# ---------------------------------------------------------------------------
# Diff endpoints: merge request vs merge commit on develop
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_git(monkeypatch):
    import select_tests

    state = {"parents": [], "merge_base": "MB"}

    def check_output(cmd, text=True):
        if cmd[:2] == ["git", "rev-list"]:
            return " ".join([cmd[-1], *state["parents"]]) + "\n"
        raise AssertionError(cmd)

    def run(cmd, **kw):
        assert cmd[:2] == ["git", "merge-base"]
        return type("R", (), {"stdout": state["merge_base"]})()

    monkeypatch.setattr(select_tests.subprocess, "check_output", check_output)
    monkeypatch.setattr(select_tests.subprocess, "run", run)
    return state


def test_endpoints_in_merge_request_use_merge_base(fake_git, monkeypatch):
    from select_tests import get_diff_endpoints

    monkeypatch.setenv("CI_COMMIT_SHA", "HEAD")
    monkeypatch.setenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "develop")
    assert get_diff_endpoints() == ("MB", "HEAD")


def test_endpoints_for_merge_commit_push_use_first_parent(fake_git, monkeypatch):
    from select_tests import get_diff_endpoints

    monkeypatch.setenv("CI_COMMIT_SHA", "HEAD")
    monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", raising=False)
    fake_git["parents"] = ["P1", "P2"]
    assert get_diff_endpoints() == ("P1", "HEAD")


def test_endpoints_for_plain_push_are_none(fake_git, monkeypatch):
    from select_tests import get_diff_endpoints

    monkeypatch.setenv("CI_COMMIT_SHA", "HEAD")
    monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", raising=False)
    fake_git["parents"] = ["P1"]
    assert get_diff_endpoints() is None


def test_plain_push_defaults_to_full_run(fake_git, monkeypatch):
    from select_tests import get_changed_files

    monkeypatch.setenv("CI_COMMIT_SHA", "HEAD")
    monkeypatch.delenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", raising=False)
    fake_git["parents"] = ["P1"]
    assert select_paths(get_changed_files(), GRAPH, None) == ["src"]


def test_full_test_run_env_forces_full_suite(monkeypatch, capsys):
    import select_tests

    monkeypatch.setenv("FULL_TEST_RUN", "true")
    select_tests.main()
    assert capsys.readouterr().out.strip() == "src"
