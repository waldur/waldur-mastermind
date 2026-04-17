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
    ".gitlab-ci.yml",
    "tests/dependency_graph.yaml",  # If the graph itself changes, run all tests.
    "src/waldur_core/server",
    "src/waldur_core/permissions",  # Nearly all apps depend on permissions.
    "conftest.py",  # Root conftest affects all tests.
    str(Path(__file__).relative_to(PROJECT_ROOT)),  # If this script changes.
]


def log(message: str):
    """Helper function to print messages to stderr."""
    print(f"[select-tests] {message}", file=sys.stderr)


def get_changed_files() -> list[str]:
    """
    Gets the list of changed files introduced by the merge request's source branch.

    This implementation is robust against merges of the target branch into the
    local checkout, ensuring that only the changes from the source branch are
    considered.
    """
    # These variables are set by GitLab at the start of the pipeline and are immutable.
    target_branch_ref = os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
    source_sha = os.environ.get("CI_COMMIT_SHA")  # The tip of the source branch.

    # This is the crucial check. If we're not in an MR pipeline, we can't do this diff.
    if not (target_branch_ref and source_sha):
        log(
            "WARNING: Not in a Merge Request pipeline context. Defaulting to full test run."
        )
        return ["pyproject.toml"]

    target_ref = f"origin/{target_branch_ref}"

    try:
        # STEP 1: Find the common ancestor (the "merge-base").
        # This is the point where the feature branch was created or last rebased
        # from the target branch.
        merge_base_cmd = ["git", "merge-base", source_sha, target_ref]
        merge_base_result = subprocess.run(
            merge_base_cmd, capture_output=True, text=True, check=True
        )
        merge_base_sha = merge_base_result.stdout.strip()

        if not merge_base_sha:
            raise ValueError("Could not determine merge-base.")

        log(
            f"Finding changes between merge-base ({merge_base_sha[:8]}) and source branch tip ({source_sha[:8]})"
        )

        # STEP 2: Diff between the merge-base and the tip of our source branch.
        # This diff will contain ONLY the changes made on the feature branch.
        # It correctly ignores any changes that were merged in from the target branch
        # during the CI job's setup.
        diff_command = ["git", "diff", "--name-only", merge_base_sha, source_sha]
        diff_result = subprocess.run(
            diff_command, capture_output=True, text=True, check=True
        )

        changed = diff_result.stdout.strip().split("\n")
        log(f"Found {len(changed)} changed file(s) specific to the source branch.")
        return changed

    except (subprocess.CalledProcessError, ValueError) as e:
        log(f"ERROR: Could not determine changed files via merge-base: {e}")
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
    if any(
        any(f.startswith(trigger) for trigger in FULL_RUN_TRIGGERS)
        for f in changed_files
    ):
        log("Core file changed, triggering a full test run.")
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
