"""
Selects a subset of tests to run based on changed files in a Git commit.

Purpose:
This script is intended to be used in a CI/CD pipeline (specifically GitLab CI)
to optimize test execution time. It determines which tests are relevant to the
changes in a given merge request, preventing the need to run the entire test suite.

How it works:
1.  It reads the `dependency_graph.yaml` file, which maps each Django application
    to a list of other applications it depends on.
2.  It builds a reverse dependency map in memory. This allows it to quickly answer
    the question: "If App B changes, which apps depend on it?"
3.  It uses GitLab's predefined CI variable `CI_MERGE_REQUEST_DIFF_BASE_SHA` to get a
    list of all files changed in the current merge request.
4.  It maps each changed file to its corresponding Django application.
5.  Build the reverse map and find all DIRECTLY affected apps.
    Transitive dependency traversal is disabled for simplicity and speed.
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


# --- Configuration Constants ---

# Define the project root as the directory containing this script.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The path to the source code directory.
SRC_ROOT = PROJECT_ROOT / "src"

# The path to the pre-computed dependency graph file.
DEPENDENCY_GRAPH_FILE = PROJECT_ROOT / "tests" / "dependency_graph.yaml"

# A list of files and directories that, if changed, are considered "core"
# changes. Any change to these will trigger a full test run as a safety measure.
FULL_RUN_TRIGGERS = [
    "pyproject.toml",
    "uv.lock",
    "tests/dependency_graph.yaml",  # If the graph itself changes, run all tests.
    "src/waldur_core/server",
    "src/waldur_core/permissions",  # Nearly all apps depend on permissions.
    "conftest.py",  # Root conftest affects all tests.
    str(Path(__file__).relative_to(PROJECT_ROOT)),  # If this script changes.
]

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


def get_diff_endpoints() -> tuple[str, str] | None:
    """Return (merge_base_sha, source_sha) for the current MR, or None if not
    running in an MR pipeline / unable to determine.
    """
    target_branch_ref = os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
    source_sha = os.environ.get("CI_COMMIT_SHA")
    if not (target_branch_ref and source_sha):
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
            "WARNING: Not in a Merge Request pipeline context. Defaulting to full test run."
        )
        return ["pyproject.toml"]

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
        return ["pyproject.toml"]


def load_dependency_graph(path: Path) -> dict[str, list[str]]:
    """
    Loads the dependency graph from the YAML file.

    Args:
        path: The path to the 'dependency_graph.yaml' file.

    Returns:
        A dictionary representing the dependency graph.
    """
    if not path.exists():
        log(f"ERROR: Dependency graph not found at {path}")
        return {}  # Return empty dict, which will trigger a full run later.

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def build_line_to_section_map(
    yaml_text: str,
) -> dict[int, tuple[str | None, str | None]]:
    """Map every 1-indexed line to (top_level_key, second_level_key).

    The second-level key is the job property a line belongs to — `rules`,
    `script`, `variables` and so on. It is needed because *which* job changed
    is not enough to decide whether the test suite must run: a diff confined to
    a job's `rules:` only changes when that job is scheduled, never what the
    Python suite does. See #293.

    The block's base indent is taken from its first indented line rather than
    assumed to be two spaces, so a differently-indented job still attributes
    correctly. Deeper lines and list items inherit the current second-level key.
    """
    result: dict[int, tuple[str | None, str | None]] = {}
    top: str | None = None
    second: str | None = None
    base_indent: int | None = None
    for line_no, line in enumerate(yaml_text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            result[line_no] = (top, second)
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            if not line.startswith("-"):
                head = line.split("#", 1)[0].rstrip()
                if ":" in head and head.split(":", 1)[0].strip():
                    top = head.split(":", 1)[0].strip()
                    second = None
                    base_indent = None
            result[line_no] = (top, second)
            continue
        if base_indent is None:
            base_indent = indent
        if indent == base_indent and not stripped.startswith("-"):
            head = stripped.split("#", 1)[0].rstrip()
            if ":" in head and head.split(":", 1)[0].strip():
                second = head.split(":", 1)[0].strip()
        result[line_no] = (top, second)
    return result


def touched_sections(
    diff_text: str, base_yaml: str, head_yaml: str
) -> dict[str, set[str | None]]:
    """Return {job_name: {second_level_keys touched}} for a unified diff."""
    base_map = build_line_to_section_map(base_yaml)
    head_map = build_line_to_section_map(head_yaml)
    sections: dict[str, set[str | None]] = {}
    for old_start, old_count, new_start, new_count in parse_diff_hunks(diff_text):
        for line_no in range(old_start, old_start + max(old_count, 1)):
            top, second = base_map.get(line_no, (None, None))
            if top:
                sections.setdefault(top, set()).add(second)
        for line_no in range(new_start, new_start + max(new_count, 1)):
            top, second = head_map.get(line_no, (None, None))
            if top:
                sections.setdefault(top, set()).add(second)
    return sections


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

    if touched & TEST_INFRASTRUCTURE_KEYS:
        log(
            "CI diff touches test infrastructure "
            f"{sorted(touched & TEST_INFRASTRUCTURE_KEYS)}; full run required."
        )
        return True

    # Walk each touched job and check its stage in the head config.
    try:
        head_config = yaml.load(head_yaml, Loader=GitLabSafeLoader) or {}
    except yaml.YAMLError as exc:
        log(f"WARNING: head .gitlab-ci.yml unparsable ({exc}); assuming full run.")
        return True

    sections = touched_sections(diff, base_yaml, head_yaml)

    for name in touched:
        if name in IMAGE_JOB_KEYS:
            log(f"Job '{name}' only builds/publishes an image; not a full-run trigger.")
            continue
        # A diff confined to a job's `rules:` only changes *when* that job is
        # scheduled — it cannot change what the Python suite does. Reached only
        # for ordinary jobs: the global-key and test-infrastructure checks above
        # have already returned, so `.unit_test_rules` (which is nothing but a
        # rules block) still forces a full run.
        if sections.get(name) == {"rules"}:
            log(f"Job '{name}': only its `rules:` changed; not a full-run trigger.")
            continue
        if resolve_stage(name, head_config) in TEST_RELATED_STAGES:
            log(f"Job '{name}' is in test-related stage; full run required.")
            return True

    log("CI diff does not touch any job that runs the test suite; skipping full run.")
    return False


def main():
    """Main execution logic."""
    # 1. Load the dependency graph from the YAML file.
    dependency_map = load_dependency_graph(DEPENDENCY_GRAPH_FILE)
    if not dependency_map:
        log("Dependency graph is empty or missing. Triggering a full test run.")
        print("src")
        return

    # 2. Get the list of files that have changed in this MR.
    changed_files = get_changed_files()
    if not changed_files or (len(changed_files) == 1 and not changed_files[0]):
        log("No changed files detected. No tests to run.")
        print("")  # Print empty string for the CI variable
        return

    # 3. Check for any "full run" triggers.
    full_run = False
    for f in changed_files:
        if any(f.startswith(trigger) for trigger in FULL_RUN_TRIGGERS):
            log(f"Core file changed: {f}, triggering a full test run.")
            full_run = True
            break

    # 3a. .gitlab-ci.yml is a conditional trigger. Only changes that affect
    # test infrastructure require a full run.
    if not full_run and ".gitlab-ci.yml" in changed_files:
        endpoints = get_diff_endpoints()
        if endpoints is None:
            log(
                ".gitlab-ci.yml changed but can't determine diff endpoints; "
                "triggering full run as a safety precaution."
            )
            full_run = True
        else:
            merge_base_sha, source_sha = endpoints
            full_run = ci_diff_affects_tests(merge_base_sha, source_sha)

    if full_run:
        print("src")
        return

    # 4. Map the changed files to their respective applications.
    all_apps = set(dependency_map.keys()) | {
        dep for deps in dependency_map.values() for dep in deps
    }
    directly_changed_apps = {
        app for f in changed_files if (app := map_file_to_app(f, all_apps))
    }

    if not directly_changed_apps:
        log(
            "Changes detected outside of any known Django app source. No tests selected."
        )
        print("")
        return

    # 5. Build the reverse map and find all affected apps via traversal.
    reverse_map = build_reverse_dependency_map(dependency_map)

    apps_to_test = set(directly_changed_apps)
    log(f"Directly changed apps: {', '.join(sorted(list(apps_to_test)))}")

    for current_app in directly_changed_apps:
        dependents = reverse_map.get(current_app, [])

        for dependent_app in dependents:
            apps_to_test.add(dependent_app)
            log(
                f"  -> Adding '{dependent_app}' because it directly depends on changed app '{current_app}'"
            )

    # 6. Convert app names back to file paths for pytest.
    test_paths = []
    for app_name in sorted(list(apps_to_test)):
        # e.g., 'waldur_mastermind.marketplace' -> 'src/waldur_mastermind/marketplace'
        app_path = SRC_ROOT / Path(*app_name.split("."))
        if app_path.exists():
            # Use relative path for the final output
            test_paths.append(str(app_path.relative_to(PROJECT_ROOT)))
        else:
            log(
                f"WARNING: Could not find directory for app '{app_name}' at expected path '{app_path}'"
            )

    if not test_paths:
        log("No testable application paths were found after analysis.")
        print("")
        return

    log("---")
    log(f"Final set of apps to test: {', '.join(sorted(list(apps_to_test)))}")

    # 7. Print the final space-separated string to stdout.
    final_output = " ".join(test_paths)
    print(final_output)


if __name__ == "__main__":
    main()
