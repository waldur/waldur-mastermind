#!/usr/bin/env python
"""
Builds a dependency graph of Django applications within the Waldur project.

Purpose:
This script performs static analysis on the Python source code to determine which
Django applications depend on others. This is crucial for optimizing CI/CD pipelines
by allowing us to run tests only for the applications that could have been affected
by a given code change.

Usage:
`select_tests.py` imports `build_dependency_map()` and computes the graph at
selection time, so there is no checked-in copy to go stale (a stale copy silently
mapped files in newly added apps to *no* tests). Running this file directly
dumps the graph as YAML for inspection.

Output Format:
The mapping has each application as a key and the list of applications it
directly depends on as the value. For example:

    waldur_mastermind.marketplace:
    - waldur_core.core
    - waldur_core.structure

This signifies that 'waldur_mastermind.marketplace' depends on 'waldur_core.core'
and 'waldur_core.structure'.
"""

import ast
import logging
import re
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
# Path(__file__).resolve() -> /path/to/project/tests/build_dependency_graph.py
# .parent                   -> /path/to/project/tests
# .parent                   -> /path/to/project/   <-- CORRECT PROJECT ROOT
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The source directory containing all Django applications.
SRC_ROOT = PROJECT_ROOT / "src"

# Directories to exclude from the analysis within each app.
EXCLUDED_DIRS = ("tests", "migrations", "management")

# Configure logging for clear and informative output.
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# The magic comment string that, if present on an import line, will cause
# the script to ignore that import for dependency tracking.
IGNORE_COMMENT_MARKER = "test-dependency-ignore"


def find_django_apps(src_path: Path) -> dict[str, Path]:
    """
    Recursively scans the source directory to find all valid Django apps.
    Identifies apps by their 'apps.py' file and extracts the canonical name.
    """
    logging.info(f"Recursively searching for Django apps in: {src_path}")
    if not src_path.is_dir():
        logging.error(f"Source directory not found at the expected path: {src_path}")
        sys.exit(1)

    apps = {}
    for app_config_path in src_path.rglob("apps.py"):
        app_dir = app_config_path.parent
        try:
            with open(app_config_path, encoding="utf-8") as f:
                content = f.read()
            match = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", content)
            if not match:
                logging.warning(
                    f"Could not find AppConfig 'name' in {app_config_path}. Skipping."
                )
                continue
            canonical_name = match.group(1)
            apps[canonical_name] = app_dir
        except Exception as e:
            logging.error(f"Failed to parse {app_config_path}: {e}")

    if not apps:
        logging.warning("No Django apps were found. The resulting graph will be empty.")
    else:
        logging.info(f"Found {len(apps)} apps.")
    return apps


def resolve_import_to_app(module_str: str | None, project_apps: set[str]) -> str | None:
    """
    Resolves a Python import string to the longest matching project app name.
    This correctly handles nested apps.
    """
    if not module_str:
        return None
    best_match = None
    for app_name in project_apps:
        if module_str == app_name or module_str.startswith(app_name + "."):
            if best_match is None or len(app_name) > len(best_match):
                best_match = app_name
    return best_match


def should_ignore_import(node: ast.AST, source_lines: list[str], marker: str) -> bool:
    """
    Checks if an import node should be ignored based on a comment marker.

    This function is robust and handles multi-line imports by checking all
    lines that the AST node spans.

    Args:
        node: The ast.Import or ast.ImportFrom node.
        source_lines: A list of all source code lines from the file.
        marker: The string to search for in a comment.

    Returns:
        True if the import should be ignored, False otherwise.
    """
    # AST line numbers are 1-based, so we subtract 1 for 0-based list indexing.
    start_line_idx = node.lineno - 1
    # Safely get the end line number; default to start line if not present.
    end_line_idx = getattr(node, "end_lineno", node.lineno) - 1

    # Check every line spanned by the import statement.
    for i in range(start_line_idx, end_line_idx + 1):
        if marker in source_lines[i]:
            return True
    return False


def build_dependency_map(src_root: Path = SRC_ROOT) -> dict[str, set[str]]:
    """Return ``{app: {apps it imports from}}`` for every app under ``src_root``.

    Apps without dependencies are present with an empty set, so the keys are
    the complete list of known apps.
    """
    project_apps_map = find_django_apps(src_root)
    project_app_names = set(project_apps_map.keys())

    dependency_map: dict[str, set[str]] = {
        app_name: set() for app_name in project_app_names
    }

    if not project_app_names:
        logging.info("Skipping code analysis as no apps were found.")
        return dependency_map
    logging.info("Starting analysis of application source code...")

    for source_app_name, app_dir in project_apps_map.items():
        for py_file in app_dir.rglob("*.py"):
            if any(excluded in py_file.parts for excluded in EXCLUDED_DIRS):
                continue
            try:
                with open(py_file, encoding="utf-8") as f:
                    source_code = f.read()
                    source_lines = source_code.splitlines()
                    tree = ast.parse(source_code, filename=py_file.name)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom | ast.Import):
                        continue

                    if should_ignore_import(node, source_lines, IGNORE_COMMENT_MARKER):
                        logging.info(
                            f"Ignoring import on line {node.lineno} in {py_file.name} due to '{IGNORE_COMMENT_MARKER}' comment."
                        )
                        continue

                    dependency_app_name = None
                    if isinstance(node, ast.ImportFrom):
                        if node.level == 0 and node.module:
                            dependency_app_name = resolve_import_to_app(
                                node.module, project_app_names
                            )
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            imported_module_name = resolve_import_to_app(
                                alias.name, project_app_names
                            )
                            if (
                                imported_module_name
                                and source_app_name != imported_module_name
                            ):
                                dependency_map[source_app_name].add(
                                    imported_module_name
                                )

                    if dependency_app_name and source_app_name != dependency_app_name:
                        dependency_map[source_app_name].add(dependency_app_name)

            except Exception as e:
                logging.warning(f"Could not parse {py_file}: {e}")

    return dependency_map


def main():
    """Print the dependency graph as YAML."""
    dependency_map = build_dependency_map()
    final_yaml_map = {
        app: sorted(deps) for app, deps in sorted(dependency_map.items()) if deps
    }
    print(yaml.dump(final_yaml_map, sort_keys=False, default_flow_style=False))
    logging.info("-" * 50)
    logging.info("Graph Generation Summary")
    logging.info(f"  - Total Apps Found: {len(dependency_map)}")
    logging.info(f"  - Apps with Dependencies: {len(final_yaml_map)}")
    logging.info(
        f"  - Total Dependency Links: {sum(len(v) for v in final_yaml_map.values())}"
    )
    logging.info("-" * 50)


if __name__ == "__main__":
    main()
