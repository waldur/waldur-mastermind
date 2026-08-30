"""
Selects a subset of tests to run based on changed files in a Git commit.

Purpose:
This script is intended to be used in a CI/CD pipeline (specifically GitLab CI)
to optimize test execution time. It determines which tests are relevant to the
changes in a given merge request, preventing the need to run the entire test suite.

How it works:
1.  It builds the app dependency graph (see build_dependency_graph.py) from the
    current source tree, so it can never be stale.
2.  It builds a reverse dependency map in memory. This allows it to quickly answer
    the question: "If App B changes, which apps depend on it?"
3.  It uses GitLab's predefined CI variable `CI_MERGE_REQUEST_DIFF_BASE_SHA` to get a
    list of all files changed in the current merge request.
4.  It drops files that cannot change the outcome of the unit suite (migrations,
    locale, docs) and maps the rest to their Django application.
5.  Each changed app is selected. Apps that directly depend on a changed app are
    added too — but only when the change is to shared code, not when it is
    confined to the app's own tests/templates/static files. Transitive
    traversal is disabled for simplicity and speed.
6.  Finally, it prints a space-separated string of the paths to the selected
    application directories. This string can be directly consumed by pytest.

Output:
- To stdout: A space-separated list of paths (e.g., "src/waldur_core src/waldur_mastermind/marketplace").
- To stderr: Informational logs about the selection process.
"""

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# --- Third-party Library Imports and Checks ---

try:
    import yaml
except ImportError:
    print("Error: 'PyYAML' library not found.", file=sys.stderr)
    print("Please install it: pip install PyYAML", file=sys.stderr)
    sys.exit(1)

# --- GitLab YAML loader -----------------------------------------------------
# GitLab CI files use custom tags like `!reference [job, section]`. Standard
# yaml.SafeLoader rejects unknown tags. We register a permissive multi-loader
# that returns None for any unknown tag, so we can still walk top-level keys.


class GitLabSafeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates unknown YAML tags (e.g. GitLab's !reference)."""


def _ignore_unknown_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


GitLabSafeLoader.add_multi_constructor("!", _ignore_unknown_tag)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_dependency_graph import build_dependency_map  # noqa: E402

# --- Configuration Constants ---

# Define the project root as the directory containing this script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The path to the source code directory.
SRC_ROOT = PROJECT_ROOT / "src"

# A list of files and directories that, if changed, are considered "core"
# changes. Any change to these will trigger a full test run as a safety measure.
#
# Deliberately NOT here (each was measured over the last 200 pipelines, #293):
#   - `src/waldur_core/permissions` — 14 of 41 MR full runs, mostly for
#     migrations and a management command. It is an ordinary app now; the
#     graph fans it out to its 25 direct dependents like any other.
#   - `pyproject.toml` — 7 full runs for version bumps and tool config. Only a
#     change to what gets installed matters; see pyproject_affects_tests().
#   - this script — its own tests are run instead, see SELECTOR_SOURCES.
FULL_RUN_TRIGGERS = [
    "uv.lock",
    "src/waldur_core/server",
    "conftest.py",  # Root conftest affects all tests.
]

# Changing the selector should exercise the selector's tests, not the suite.
SELECTOR_SOURCES = ("tests/select_tests.py", "tests/build_dependency_graph.py")
SELECTOR_TESTS = "tests/test_select_tests.py"

# Sections of pyproject.toml whose change cannot alter what the unit suite
# does. Anything outside this list (dependencies, dependency-groups,
# entry-points, tool.uv, tool.pytest, ...) forces a full run.
PYPROJECT_IRRELEVANT_KEYS = (
    ("project", "version"),
    ("project", "description"),
    ("project", "readme"),
    ("tool", "ruff"),
    ("tool", "pyright"),
    ("tool", "mypy"),
)

# Directory names / suffixes of files that cannot change the unit suite's
# outcome. The suite runs with --no-migrations, so migrations are covered by
# `Run migration tests`, not here. A migration plus its test cost a 279-minute
# 51-app run before this filter existed.
IRRELEVANT_DIRS = {"migrations", "locale"}
IRRELEVANT_SUFFIXES = {".md", ".po", ".mo"}

# A change confined to these directories affects only the owning app's tests:
# nothing in another app imports them. Such a change selects the app itself
# but does not fan out to its dependents. (`management` is *not* here — 33
# test files call other apps' commands via call_command.)
LOCAL_ONLY_DIRS = {"tests", "templates", "static"}

# .gitlab-ci.yml is treated specially — see ci_diff_affects_tests().
# Only changes that touch test/build stages or test-related sections trigger
# a full run. Pure SDK/release/deploy changes don't.

# Names of GitLab CI stages whose jobs run code that needs validation.
TEST_RELATED_STAGES = {"test", "build", "predeploy"}

# Top-level keys in .gitlab-ci.yml whose changes affect every job globally.
# Conservative — any touch triggers a full run.
GLOBAL_CI_KEYS = {
    "include",
    "default",
    "variables",
    "stages",
    "workflow",
    "before_script",
    "after_script",
    "image",
    "services",
    "cache",
}

# YAML anchors / job names that are part of test infrastructure. Touching
# any of these triggers a full run regardless of stage.
TEST_INFRASTRUCTURE_KEYS = {
    ".unit_test_rules",
    ".Unit test runner",
    "Generate test pipeline",
    "Run tests dynamically",
}

# Jobs that build or publish container images. They live in the `test` and
# `build` stages, but they do not run the Python suite and nothing about their
# definition can change its outcome — so a change confined to them must not
# force a full run. Without this, editing the `rules:` of a buildah job cost a
# 15-shard pytest run (~277 runner-minutes); see #293.
IMAGE_JOB_KEYS = {
    "Build docker image for tests",
    "Build MR image for integration tests",
    "Publish the YOLO multiarch docker image",
    "Publish the latest multiarch docker image",
    "Publish multiarch docker image with specific version",
    "Test Multi-arch docker image build",
    "Try building docker image",
    "Lint docker image",
    "Lint dockerfile",
}


def resolve_stage(
    name: str, config: dict, _seen: frozenset[str] = frozenset()
) -> str | None:
    """Return a job's stage, following `extends:` when it is not set inline.

    `body.get("stage")` alone silently returns None for every job that inherits
    its stage — in this repo that is everything extending `.Unit test runner`
    (`Run migration tests`, `Run demo presets tests`, `Run type checkings`,
    `Check startup memory budget`). They were then reported as "limited to
    deploy/postdeploy/release jobs", which is the opposite of the truth.
    """
    body = config.get(name)
    if not isinstance(body, dict) or name in _seen:
        return None
    if body.get("stage") is not None:
        return body["stage"]
    extends = body.get("extends")
    if isinstance(extends, str):
        extends = [extends]
    for parent in extends or []:
        stage = resolve_stage(parent, config, _seen | {name})
        if stage is not None:
            return stage
    return None


def log(message: str):
    """Helper function to print messages to stderr."""
    print(f"[select-tests] {message}", file=sys.stderr)


def _first_parent(sha: str) -> str | None:
    """Return ``sha^1`` if ``sha`` is a merge commit, else None."""
    try:
        parents = subprocess.check_output(
            ["git", "rev-list", "--parents", "-n", "1", sha], text=True
        ).split()[1:]
    except subprocess.CalledProcessError:
        return None
    return parents[0] if len(parents) >= 2 else None


def get_diff_endpoints() -> tuple[str, str] | None:
    """Return (base_sha, head_sha) describing the change under test.

    - Merge request pipeline: merge-base of the source branch and the target,
      so only the MR's own commits count.
    - Push of a merge commit (an MR landing on develop): the merge commit's
      first parent, i.e. the same files the MR changed — but selected and run
      against the *actual* post-merge tree. That is what catches two MRs that
      each passed alone but conflict together, without replaying the whole
      suite on every merge (which cost ~178 runner-hours per 5 days, #293).
    - Anything else (direct push of plain commits, no git context): None,
      which the caller treats as a full run.
    """
    target_branch_ref = os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
    source_sha = os.environ.get("CI_COMMIT_SHA")
    if not source_sha:
        return None
    if not target_branch_ref:
        parent = _first_parent(source_sha)
        if parent:
            log(f"Merge commit pushed; diffing against first parent {parent[:8]}.")
            return parent, source_sha
        return None
    target_ref = f"origin/{target_branch_ref}"
    try:
        result = subprocess.run(
            ["git", "merge-base", source_sha, target_ref],
            capture_output=True,
            text=True,
            check=True,
        )
        merge_base_sha = result.stdout.strip()
        if not merge_base_sha:
            return None
        return merge_base_sha, source_sha
    except subprocess.CalledProcessError:
        return None


def get_changed_files() -> list[str]:
    """
    Gets the list of changed files introduced by the merge request's source branch.

    This implementation is robust against merges of the target branch into the
    local checkout, ensuring that only the changes from the source branch are
    considered.
    """
    endpoints = get_diff_endpoints()
    if endpoints is None:
        log(
            "WARNING: Neither a merge request nor a merge commit. Defaulting to full test run."
        )
        return ["uv.lock"]

    merge_base_sha, source_sha = endpoints
    log(
        f"Finding changes between merge-base ({merge_base_sha[:8]}) and source branch tip ({source_sha[:8]})"
    )

    try:
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", merge_base_sha, source_sha],
            capture_output=True,
            text=True,
            check=True,
        )
        changed = diff_result.stdout.strip().split("\n")
        log(f"Found {len(changed)} changed file(s) specific to the source branch.")
        return changed
    except subprocess.CalledProcessError as e:
        log(f"ERROR: Could not determine changed files: {e}")
        log("Defaulting to full test run as a safety precaution.")
        return ["uv.lock"]


def build_reverse_dependency_map(
    dependency_map: dict[str, list[str]],
) -> dict[str, list[str]]:
    """
    Inverts the dependency graph.

    The input map shows `app -> [dependencies]`.
    The output map shows `dependency -> [apps that depend on it]`.

    Args:
        dependency_map: The original dependency graph.

    Returns:
        A new dictionary representing the reverse graph.
    """
    reverse_map = {}
    for app, dependencies in dependency_map.items():
        for dep in dependencies:
            reverse_map.setdefault(dep, []).append(app)
    return reverse_map


def map_file_to_app(file_path: str, all_apps: set[str]) -> str | None:
    """
    Maps a file path to its corresponding canonical Django app name.

    It finds the longest matching app name that is a prefix of the file's path.
    e.g., 'src/waldur_mastermind/marketplace/models.py' -> 'waldur_mastermind.marketplace'

    Args:
        file_path: The path of the changed file.
        all_apps: A set of all known canonical app names.

    Returns:
        The canonical app name if a match is found, otherwise None.
    """
    if not file_path.startswith("src/"):
        return None

    # Convert file path to a module-like path for comparison
    module_like_path = file_path.replace("src/", "").replace("/", ".")

    best_match = None
    for app_name in all_apps:
        if module_like_path.startswith(app_name):
            if best_match is None or len(app_name) > len(best_match):
                best_match = app_name
    return best_match


def is_test_irrelevant(file_path: str) -> bool:
    """True for files that cannot change what the unit suite does."""
    path = Path(file_path)
    return bool(IRRELEVANT_DIRS & set(path.parts)) or path.suffix in IRRELEVANT_SUFFIXES


def is_local_only(file_path: str) -> bool:
    """True when a change to this file can only affect its own app's tests."""
    return bool(LOCAL_ONLY_DIRS & set(Path(file_path).parts))


def _drop_keys(data: dict, keys: tuple[tuple[str, ...], ...]) -> dict:
    for path in keys:
        node = data
        for part in path[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return data


def pyproject_affects_tests(base_text: str | None, head_text: str | None) -> bool:
    """True unless the pyproject.toml change is confined to PYPROJECT_IRRELEVANT_KEYS.

    Unparsable or missing content is treated as affecting tests.
    """
    if base_text is None or head_text is None:
        return True
    try:
        base = _drop_keys(tomllib.loads(base_text), PYPROJECT_IRRELEVANT_KEYS)
        head = _drop_keys(tomllib.loads(head_text), PYPROJECT_IRRELEVANT_KEYS)
    except tomllib.TOMLDecodeError as exc:
        log(f"WARNING: pyproject.toml unparsable ({exc}); assuming full run.")
        return True
    return base != head


def _git_show(sha: str, path: str) -> str | None:
    try:
        return subprocess.check_output(["git", "show", f"{sha}:{path}"], text=True)
    except subprocess.CalledProcessError:
        return None


def pyproject_diff_affects_tests(merge_base_sha: str, head_sha: str) -> bool:
    return pyproject_affects_tests(
        _git_show(merge_base_sha, "pyproject.toml"),
        _git_show(head_sha, "pyproject.toml"),
    )


def build_line_to_top_level_key_map(yaml_text: str) -> dict[int, str | None]:
    """Map every 1-indexed line number in a YAML document to its enclosing
    top-level key.

    A "top-level key" is any line that begins at column 0 and contains a colon,
    representing a job, anchor, or global directive. Lines inside the body of
    a top-level key inherit that key. Empty lines and comments inherit the
    most recent key seen so far.

    Examples:
      "Generate OpenAPI schema:" → top-level key "Generate OpenAPI schema"
      "  stage: deploy"          → child of the most recent top-level key
      ".unit_test_rules: &x"     → top-level key ".unit_test_rules"
    """
    result: dict[int, str | None] = {}
    current_key: str | None = None
    for line_no, line in enumerate(yaml_text.split("\n"), start=1):
        if not line or line[0].isspace() or line.startswith("#"):
            result[line_no] = current_key
            continue
        # Top-level: starts at column 0. Could be a key or a list item.
        # We only care about keys (`name:` form), not list items (`- ...`).
        if line.startswith("-"):
            result[line_no] = current_key
            continue
        # Strip comment after a possible value
        head = line.split("#", 1)[0].rstrip()
        if ":" in head:
            key_part = head.split(":", 1)[0].strip()
            if key_part:
                current_key = key_part
        result[line_no] = current_key
    return result


def changed_job_keys(
    name: str, base_config: dict, head_config: dict
) -> set[str] | None:
    """Return the set of keys whose value differs between the base and head
    definition of `name`, or None if the key is absent from both.

    This is a *semantic* comparison of the parsed job, not of diff lines, which
    matters for two reasons the earlier line-based attribution got wrong:

      - PyYAML resolves `<<:` merge keys, so swapping which rules anchor a job
        merges shows up here as a plain change to `rules`, even though the diff
        touches a line reading `<<: *some_anchor`.
      - A comment added above a job — or anywhere inside it — changes no key at
        all, so the job is correctly reported as unchanged. Line attribution
        blamed such comments on whichever key preceded them.

    See #293.
    """
    base = base_config.get(name)
    head = head_config.get(name)
    if base is None and head is None:
        return None
    if not isinstance(base, dict) or not isinstance(head, dict):
        # Added, removed, or changed shape — treat every key as changed. The
        # fallback keeps the result non-empty so an addition is never mistaken
        # for "semantically unchanged" and pruned.
        keys: set[str] = set()
        for side in (base, head):
            if isinstance(side, dict):
                keys |= set(side)
        return keys or {"__whole_key__"}
    return {k for k in set(base) | set(head) if base.get(k) != head.get(k)}


def parse_diff_hunks(diff_text: str) -> list[tuple[int, int, int, int]]:
    """Parse a unified diff and return the (old_start, old_count, new_start,
    new_count) tuple for each hunk.

    For omitted counts (default 1), the count is filled in.
    """
    hunks = []
    pattern = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for line in diff_text.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        hunks.append((old_start, old_count, new_start, new_count))
    return hunks


def collect_touched_top_level_keys(
    diff_text: str, base_yaml: str, head_yaml: str
) -> set[str]:
    """Return the set of top-level YAML keys touched by the diff."""
    base_map = build_line_to_top_level_key_map(base_yaml)
    head_map = build_line_to_top_level_key_map(head_yaml)
    touched: set[str] = set()
    for old_start, old_count, new_start, new_count in parse_diff_hunks(diff_text):
        for ln in range(old_start, old_start + old_count):
            k = base_map.get(ln)
            if k:
                touched.add(k)
        for ln in range(new_start, new_start + new_count):
            k = head_map.get(ln)
            if k:
                touched.add(k)
    return touched


def ci_diff_affects_tests(merge_base_sha: str, head_sha: str) -> bool:
    """Return True if .gitlab-ci.yml changes between two SHAs touch
    test infrastructure and a full test run is therefore required.

    Conservatively returns True on any error (parse failure, git failure,
    unexpected structure) so we never silently skip tests.
    """
    try:
        diff = subprocess.check_output(
            [
                "git",
                "diff",
                "-U0",
                f"{merge_base_sha}..{head_sha}",
                "--",
                ".gitlab-ci.yml",
            ],
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        log(f"WARNING: git diff failed for .gitlab-ci.yml: {exc}; assuming full run.")
        return True

    if not diff.strip():
        return False  # No changes to .gitlab-ci.yml after all

    try:
        base_yaml = subprocess.check_output(
            ["git", "show", f"{merge_base_sha}:.gitlab-ci.yml"], text=True
        )
    except subprocess.CalledProcessError:
        base_yaml = ""
    try:
        head_yaml = subprocess.check_output(
            ["git", "show", f"{head_sha}:.gitlab-ci.yml"], text=True
        )
    except subprocess.CalledProcessError:
        head_yaml = ""

    touched = collect_touched_top_level_keys(diff, base_yaml, head_yaml)
    log(f"CI diff touches top-level keys: {sorted(touched)}")

    if not touched:
        log("WARNING: No top-level keys attributed; assuming full run.")
        return True

    if touched & GLOBAL_CI_KEYS:
        log(
            "CI diff touches global keys "
            f"{sorted(touched & GLOBAL_CI_KEYS)}; full run required."
        )
        return True

    # Parse both sides so the line-attributed key set can be checked against what
    # actually changed. Line attribution is deliberately generous — it blames a
    # comment on the key it follows — so it reports keys the diff never altered.
    try:
        head_config = yaml.load(head_yaml, Loader=GitLabSafeLoader) or {}
        base_config = yaml.load(base_yaml, Loader=GitLabSafeLoader) or {}
    except yaml.YAMLError as exc:
        log(f"WARNING: .gitlab-ci.yml unparsable ({exc}); assuming full run.")
        return True

    # Drop keys whose parsed definition is identical on both sides. Without this,
    # adding a documented anchor after `.unit_test_rules` blamed the new comment
    # block on `.unit_test_rules` and forced a full run, even though that key was
    # untouched.
    unchanged = {
        n for n in touched if changed_job_keys(n, base_config, head_config) == set()
    }
    if unchanged:
        log(f"Keys attributed by line but semantically unchanged: {sorted(unchanged)}")
        touched = touched - unchanged
        if not touched:
            log("Nothing actually changed in the CI file; skipping full run.")
            return False

    if touched & TEST_INFRASTRUCTURE_KEYS:
        log(
            "CI diff touches test infrastructure "
            f"{sorted(touched & TEST_INFRASTRUCTURE_KEYS)}; full run required."
        )
        return True

    for name in touched:
        if name in IMAGE_JOB_KEYS:
            log(f"Job '{name}' only builds/publishes an image; not a full-run trigger.")
            continue
        # `rules:` only controls *when* a job is scheduled — it cannot change what
        # the Python suite does. Compared semantically, so swapping which rules
        # anchor a job merges (`<<: *other_anchor`) counts as a rules change even
        # though the diff line reads `<<`. Reached only for ordinary jobs: the
        # global-key and test-infrastructure checks above have already returned,
        # so `.unit_test_rules` — which is nothing but a rules block — still
        # forces a full run.
        if changed_job_keys(name, base_config, head_config) == {"rules"}:
            log(f"Job '{name}': only its `rules:` changed; not a full-run trigger.")
            continue
        if resolve_stage(name, head_config) in TEST_RELATED_STAGES:
            log(f"Job '{name}' is in test-related stage; full run required.")
            return True

    log("CI diff does not touch any job that runs the test suite; skipping full run.")
    return False


def select_paths(
    changed_files: list[str],
    dependency_map: dict[str, set[str]],
    endpoints: tuple[str, str] | None,
) -> list[str]:
    """Decide which pytest paths to run. Pure: all git access goes through
    the callbacks used by the *_diff_affects_tests helpers, which take SHAs.

    Returns ``["src"]`` for a full run and ``[]`` for nothing to run.
    """
    changed_files = [f for f in changed_files if f]
    if not changed_files:
        log("No changed files detected. No tests to run.")
        return []

    for f in changed_files:
        if any(f.startswith(trigger) for trigger in FULL_RUN_TRIGGERS):
            log(f"Core file changed: {f}, triggering a full test run.")
            return ["src"]

    # Conditional triggers: only the parts that can reach the suite count.
    conditional = {
        ".gitlab-ci.yml": ci_diff_affects_tests,
        "pyproject.toml": pyproject_diff_affects_tests,
    }
    for name, check in conditional.items():
        if name not in changed_files:
            continue
        if endpoints is None:
            log(
                f"{name} changed but can't determine diff endpoints; "
                "triggering full run as a safety precaution."
            )
            return ["src"]
        if check(*endpoints):
            return ["src"]

    extra_paths = []
    if any(f in SELECTOR_SOURCES for f in changed_files):
        log("Test selector changed; adding its own tests.")
        extra_paths.append(SELECTOR_TESTS)

    all_apps = set(dependency_map)
    relevant = [f for f in changed_files if not is_test_irrelevant(f)]
    ignored = sorted(set(changed_files) - set(relevant))
    if ignored:
        log(f"Ignoring {len(ignored)} file(s) that cannot affect unit tests: {ignored}")

    directly_changed_apps: set[str] = set()
    shared_change_apps: set[str] = set()
    for f in relevant:
        app = map_file_to_app(f, all_apps)
        if not app:
            continue
        directly_changed_apps.add(app)
        if not is_local_only(f):
            shared_change_apps.add(app)

    if not directly_changed_apps:
        log(
            "Changes detected outside of any known Django app source. No app tests selected."
        )
        return extra_paths

    reverse_map = build_reverse_dependency_map(dependency_map)

    apps_to_test = set(directly_changed_apps)
    log(f"Directly changed apps: {', '.join(sorted(apps_to_test))}")
    local_only = directly_changed_apps - shared_change_apps
    if local_only:
        log(
            "Only tests/templates/static changed in "
            f"{', '.join(sorted(local_only))}; not fanning out to dependents."
        )

    for current_app in sorted(shared_change_apps):
        for dependent_app in reverse_map.get(current_app, []):
            apps_to_test.add(dependent_app)
            log(
                f"  -> Adding '{dependent_app}' because it directly depends on changed app '{current_app}'"
            )

    test_paths = []
    for app_name in sorted(apps_to_test):
        app_path = SRC_ROOT / Path(*app_name.split("."))
        if app_path.exists():
            test_paths.append(str(app_path.relative_to(PROJECT_ROOT)))
        else:
            log(
                f"WARNING: Could not find directory for app '{app_name}' at expected path '{app_path}'"
            )

    log("---")
    log(f"Final set of apps to test: {', '.join(sorted(apps_to_test))}")
    return extra_paths + test_paths


def main():
    """Main execution logic."""
    if os.environ.get("FULL_TEST_RUN", "").lower() in ("true", "yes"):
        log("FULL_TEST_RUN is set (scheduled sweep); running the whole suite.")
        print("src")
        return

    dependency_map = build_dependency_map()
    if not dependency_map:
        log("Dependency graph is empty. Triggering a full test run.")
        print("src")
        return

    changed_files = get_changed_files()
    print(" ".join(select_paths(changed_files, dependency_map, get_diff_endpoints())))


if __name__ == "__main__":
    main()
