#!/usr/bin/env python

import argparse
import ast
import json
from pathlib import Path


def find_argument_spec(file_path: Path):
    """Parses a Python file and extracts the 'argument_spec' or 'ARGUMENT_SPEC' dictionary."""
    try:
        with open(file_path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(file_path))
    except (SyntaxError, FileNotFoundError):
        return None

    spec_variable_names = {"argument_spec", "ARGUMENT_SPEC"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in spec_variable_names
        ):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def get_all_specs_by_collection(root_dir: Path):
    """Scans for Ansible collections and extracts argument_spec for all modules."""
    collections_data = {}
    collections_root = root_dir / "ansible_collections"
    if not collections_root.is_dir():
        return collections_data

    for namespace_dir in collections_root.iterdir():
        if not namespace_dir.is_dir():
            continue
        for collection_dir in namespace_dir.iterdir():
            if not collection_dir.is_dir():
                continue
            collection_name = f"{namespace_dir.name}.{collection_dir.name}"
            module_dir = collection_dir / "plugins" / "modules"
            if module_dir.is_dir():
                specs = {}
                for py_file in module_dir.glob("*.py"):
                    spec = find_argument_spec(py_file)
                    if spec:
                        specs[py_file.stem] = spec
                if specs:
                    collections_data[collection_name] = specs
    return collections_data


def find_breaking_changes_in_module(old_spec, new_spec):
    """
    Compares the argument specs of a single module and returns a list of
    strings describing only the breaking changes.
    """
    breaking_changes = []
    all_params = set(old_spec.keys()) | set(new_spec.keys())

    for param in sorted(all_params):
        is_new = param not in old_spec
        is_removed = param not in new_spec

        if is_removed:
            breaking_changes.append(f"**Removed parameter:** `{param}`")
            continue

        new_attrs = new_spec.get(param, {})
        if is_new:
            # BREAKING: A new parameter was added and it is mandatory.
            if new_attrs.get("required", False):
                breaking_changes.append(f"**Added mandatory parameter:** `{param}`")
            continue

        # If we reach here, the parameter exists in both old and new specs.
        old_attrs = old_spec.get(param, {})
        if old_attrs == new_attrs:
            continue

        param_specific_changes = []
        old_req, new_req = (
            old_attrs.get("required", False),
            new_attrs.get("required", False),
        )
        old_type, new_type = old_attrs.get("type"), new_attrs.get("type")
        old_ro, new_ro = (
            old_attrs.get("read_only", False),
            new_attrs.get("read_only", False),
        )
        old_choices = set(old_attrs.get("choices") or [])
        new_choices = set(new_attrs.get("choices") or [])

        # BREAKING: An optional parameter became mandatory.
        if not old_req and new_req:
            param_specific_changes.append("is now **mandatory**")

        # BREAKING: The parameter type has changed.
        if old_type != new_type:
            param_specific_changes.append(
                f"type changed from `{old_type}` to `{new_type}`"
            )

        # BREAKING: A writable parameter became read-only.
        if not old_ro and new_ro:
            param_specific_changes.append("is now **read-only**")

        # BREAKING: A value was removed from the list of choices.
        if old_choices and not old_choices.issubset(new_choices):
            removed = sorted(list(old_choices - new_choices))
            param_specific_changes.append(
                f"the following choices were removed: `{', '.join(removed)}`"
            )

        if param_specific_changes:
            breaking_changes.append(
                f"**Parameter `{param}`:** {', '.join(param_specific_changes)}."
            )

    return breaking_changes


def generate_markdown_for_collection(changelog, version):
    """
    Generates and returns a Markdown string for a collection's breaking changes,
    including a definition of what is considered a breaking change.
    """
    # If there are no breaking changes to report, return an empty string.
    if not any(changelog.values()):
        return ""

    # The main header for the changelog section.
    lines = [f"## Breaking Changes for {version}\n"]

    clarification_text = """
> **Note:** This changelog only lists breaking changes that could cause an existing playbook to fail. Non-breaking changes, such as adding a new optional parameter or a new module, are not included.
>
> A change is considered breaking if:
> - A module or a parameter is removed.
> - A new mandatory parameter is added.
> - An existing parameter becomes mandatory.
> - A parameter's data type is changed.
> - A writable parameter becomes read-only.
> - A value is removed from a parameter's `choices`.
"""
    lines.append(clarification_text)

    # Append the actual list of breaking changes.
    if changelog.get("removed_modules"):
        lines.append("### 🗑️ Removed Modules")
        lines.extend([f"- {m}" for m in changelog["removed_modules"]])

    if changelog.get("modified_modules"):
        lines.append("### 🔄 Modified Modules with Breaking Changes")
        for name, details in changelog["modified_modules"].items():
            lines.append(f"#### `{name}`")
            lines.extend([f"- {change}" for change in details])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a breaking changes changelog for Ansible collections."
    )
    parser.add_argument(
        "old_dir", help="Path to the root directory with the old collections."
    )
    parser.add_argument(
        "new_dir", help="Path to the root directory with the new collections."
    )
    parser.add_argument(
        "--version", help="Release version for the changelog title.", default="latest"
    )
    args = parser.parse_args()

    old_collections = get_all_specs_by_collection(Path(args.old_dir))
    new_collections = get_all_specs_by_collection(Path(args.new_dir))
    all_collection_names = set(old_collections.keys()) | set(new_collections.keys())

    collection_changelogs = {}

    for name in sorted(all_collection_names):
        old_modules = old_collections.get(name, {})
        new_modules = new_collections.get(name, {})

        changelog = {"removed_modules": [], "modified_modules": {}}

        all_module_names = set(old_modules.keys()) | set(new_modules.keys())
        for module_name in sorted(all_module_names):
            if module_name not in new_modules:
                # BREAKING: Module was removed.
                changelog["removed_modules"].append(f"`{module_name}`")
                continue

            if module_name not in old_modules:
                # NOT BREAKING: A new module was added. Skip.
                continue

            breaking_changes = find_breaking_changes_in_module(
                old_modules[module_name], new_modules[module_name]
            )
            if breaking_changes:
                changelog["modified_modules"][module_name] = breaking_changes

        markdown_output = generate_markdown_for_collection(changelog, args.version)
        if markdown_output:
            collection_changelogs[name] = markdown_output

    print(json.dumps(collection_changelogs, indent=2))


if __name__ == "__main__":
    main()
