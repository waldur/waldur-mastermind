#!/usr/bin/env python3
"""
Analyze which test classes inheriting from APITransactionTestCase can be
safely migrated to APITestCase.

APITransactionTestCase truncates all DB tables between tests, which is much
slower than APITestCase's transaction rollback approach. Most test classes
do not actually need TransactionTestCase semantics.

This script uses Python's ast module to parse test files and detect patterns
that genuinely require TransactionTestCase:
  - on_commit usage
  - select_for_update
  - transaction.atomic in test methods
  - serialized_rollback attribute
  - threading / Thread usage
  - Celery .delay() / .apply_async() / .apply() calls (unmocked)
  - assertNumQueries (flagged for review, may behave differently)

Classes inheriting from custom base classes (not directly from
APITransactionTestCase) are flagged as REVIEW since their base class
would also need migration.

Usage:
    python scripts/analyze_transaction_test_cases.py
"""

import argparse
import ast
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Classification constants
# ---------------------------------------------------------------------------

SAFE = "SAFE"
REVIEW = "REVIEW"
UNSAFE = "UNSAFE"

# Patterns that definitely require TransactionTestCase
UNSAFE_PATTERNS = {
    "on_commit": "transaction.on_commit usage",
    "select_for_update": "select_for_update requires real transactions",
    "transaction.atomic": "explicit transaction management",
    "serialized_rollback": "TransactionTestCase-specific attribute",
    "threading": "multi-threaded DB access",
    "celery_task_exec": "Celery task execution (.delay/.apply_async/.apply)",
    "integrity_error": "IntegrityError breaks TestCase's wrapping transaction",
    "responses_start_in_setup": "responses.start() in setUp leaks across TestCase classes",
}

# Patterns that need manual review
REVIEW_PATTERNS = {
    "assertNumQueries": "may behave differently with TestCase wrapping",
    "custom_base_class": "inherits from custom base, not directly from APITransactionTestCase",
    "task_always_eager": "task_always_eager=True often implies on_commit dependency",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ClassResult:
    """Analysis result for a single test class."""

    file_path: str
    class_name: str
    line_number: int
    classification: str  # SAFE, REVIEW, UNSAFE
    reasons: list = field(default_factory=list)

    @property
    def full_name(self):
        return f"{self.file_path}::{self.class_name}"

    @property
    def app_name(self):
        """Extract the app name from the file path, e.g. 'waldur_core/structure'."""
        parts = Path(self.file_path).parts
        # Typical path: src/waldur_foo/bar/tests/test_x.py
        # We want: waldur_foo/bar
        try:
            src_idx = list(parts).index("src")
        except ValueError:
            return "unknown"
        # Collect parts after src/ up to (but not including) "tests"
        app_parts = []
        for part in parts[src_idx + 1 :]:
            if part == "tests":
                break
            app_parts.append(part)
        return "/".join(app_parts) if app_parts else "unknown"


# ---------------------------------------------------------------------------
# AST analysis helpers
# ---------------------------------------------------------------------------


def _resolve_import_aliases(tree):
    """
    Walk the module-level imports and return two dicts:

    1. module_imports: maps alias -> module name for `import X` and `import X as Y`
       e.g. {"test": "rest_framework.test"} for `from rest_framework import test`
       (this is technically a from-import, handled in from_imports)

    2. from_imports: maps (module, alias) for `from X import Y`
       Stored as alias -> (module, original_name)
       e.g. {"APITransactionTestCase": ("rest_framework.test", "APITransactionTestCase")}
    """
    module_imports = {}  # alias -> full module
    from_imports = {}  # alias -> (module, original_name)

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                module_imports[name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                from_imports[name] = (module, alias.name)

    return module_imports, from_imports


def _is_api_transaction_test_case(base_node, from_imports):
    """
    Check whether an AST base class node refers to APITransactionTestCase.

    Handles two patterns:
    1. test.APITransactionTestCase  (Attribute node, when `from rest_framework import test`)
    2. APITransactionTestCase       (Name node, when `from rest_framework.test import APITransactionTestCase`)

    Returns:
        True if the base is APITransactionTestCase (direct inheritance)
        False otherwise
    """
    if isinstance(base_node, ast.Attribute):
        # Pattern: test.APITransactionTestCase
        if base_node.attr == "APITransactionTestCase" and isinstance(
            base_node.value, ast.Name
        ):
            alias = base_node.value.id
            # Check that `test` was imported from rest_framework
            if alias in from_imports:
                module, original_name = from_imports[alias]
                if module == "rest_framework" and original_name == "test":
                    return True
            # Also handle plain `import rest_framework.test as test` (rare)
            return False

    elif isinstance(base_node, ast.Name):
        # Pattern: APITransactionTestCase (direct import)
        alias = base_node.id
        if alias in from_imports:
            module, original_name = from_imports[alias]
            if (
                module == "rest_framework.test"
                and original_name == "APITransactionTestCase"
            ):
                return True

    return False


def _has_transaction_import(from_imports):
    """Check if `from django.db import transaction` is present."""
    for alias, (module, original_name) in from_imports.items():
        if module == "django.db" and original_name == "transaction":
            return True
    return False


def _has_threading_import(from_imports, module_imports):
    """Check if threading module is imported at file level."""
    if "threading" in module_imports:
        return True
    for alias, (module, original_name) in from_imports.items():
        if module == "threading" or (module == "" and original_name == "threading"):
            return True
    return False


class PatternDetector(ast.NodeVisitor):
    """
    Visit an AST subtree (class body) and detect patterns that require
    TransactionTestCase.
    """

    def __init__(self, has_transaction_import=False):
        self.found_patterns = set()
        self._has_transaction_import = has_transaction_import

    def visit_Attribute(self, node):
        # Detect: transaction.on_commit, transaction.atomic
        if isinstance(node.value, ast.Name) and node.value.id == "transaction":
            if node.attr == "on_commit":
                self.found_patterns.add("on_commit")
            elif node.attr == "atomic":
                self.found_patterns.add("transaction.atomic")

        # Detect: qs.select_for_update()
        if node.attr == "select_for_update":
            self.found_patterns.add("select_for_update")

        # Detect: assertNumQueries
        if node.attr == "assertNumQueries":
            self.found_patterns.add("assertNumQueries")

        # Detect: .delay(), .apply_async() — actual Celery task execution.
        # We skip patterns that are mock assertions (e.g., mock_task.delay.call_count,
        # mock_task.delay.assert_called_once_with) by checking that .delay/.apply_async
        # is the outermost attribute (i.e., it's being called, not accessed for assertions).
        if node.attr in ("delay", "apply_async"):
            # Check if this is a direct call: something.delay(...)
            # We'll mark it, then filter out mock patterns in a parent Call check
            if not self._is_mock_assertion(node):
                self.found_patterns.add("celery_task_exec")

        self.generic_visit(node)

    @staticmethod
    def _is_mock_assertion(attr_node):
        """
        Check if an attribute access like .delay is part of a mock assertion chain,
        e.g. mock_tasks.send_email.delay.call_count or
             mock_tasks.send_email.delay.assert_called_once_with(...)
        These are NOT real Celery calls and should not flag the class as UNSAFE.

        Also returns True if the .delay is accessed on an object whose name starts
        with 'mock' (e.g., mock_task.delay.call_args).
        """

        # Walk up the attribute chain to look for mock indicators
        # Pattern: X.delay is the value of a parent Attribute node
        # (e.g., X.delay.call_count, X.delay.assert_called_once_with)
        # We can't easily walk up in the AST, so we check the value chain downward:
        # If attr_node.value contains 'mock' in any Name node, it's likely a mock.
        def _has_mock_name(node):
            if isinstance(node, ast.Name):
                return "mock" in node.id.lower()
            if isinstance(node, ast.Attribute):
                return "mock" in node.attr.lower() or _has_mock_name(node.value)
            return False

        return _has_mock_name(attr_node.value)

    def visit_Name(self, node):
        # Detect: on_commit used as a bare name (less common but possible)
        if node.id == "on_commit":
            self.found_patterns.add("on_commit")
        # Detect: serialized_rollback as a name
        if node.id == "serialized_rollback":
            self.found_patterns.add("serialized_rollback")
        # Detect: Thread used as a name
        if node.id == "Thread":
            self.found_patterns.add("threading")
        # Detect: IntegrityError — raising this in TestCase breaks the transaction
        if node.id == "IntegrityError":
            self.found_patterns.add("integrity_error")
        self.generic_visit(node)

    def visit_Assign(self, node):
        # Detect: serialized_rollback = True (class-level attribute)
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "serialized_rollback":
                self.found_patterns.add("serialized_rollback")
        self.generic_visit(node)

    def visit_Call(self, node):
        # Detect: threading.Thread(...)
        if isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "Thread"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "threading"
            ):
                self.found_patterns.add("threading")
        # Detect: Thread(...)
        elif isinstance(node.func, ast.Name) and node.func.id == "Thread":
            self.found_patterns.add("threading")
        self.generic_visit(node)

    def visit_Import(self, node):
        # Detect: `import threading` inside a method body
        for alias in node.names:
            if alias.name == "threading":
                self.found_patterns.add("threading")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        # Detect: `from threading import Thread` inside a method body
        if node.module == "threading":
            self.found_patterns.add("threading")
        self.generic_visit(node)

    def visit_Constant(self, node):
        # We skip string constants to avoid false positives from docstrings/comments
        self.generic_visit(node)


def _check_mock_decorators_for_on_commit(class_node):
    """
    Check if on_commit references in the class are inside @patch decorators
    (meaning they are mocked, not actually using on_commit behavior).

    Returns True if ALL on_commit references appear to be in mock contexts.
    """
    # This is a heuristic - we look for @patch decorators containing "on_commit"
    on_commit_in_patch = 0
    on_commit_total = 0

    for node in ast.walk(class_node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "on_commit" in node.value:
                # Check if this string is part of a @patch decorator argument
                on_commit_total += 1
                # We'll count it as patched (this is a simplification)
                on_commit_in_patch += 1

    return on_commit_total > 0 and on_commit_in_patch == on_commit_total


def analyze_class(class_node, from_imports, module_imports):
    """
    Analyze a single class node for patterns requiring TransactionTestCase.

    Returns a set of pattern names found.
    """
    has_txn_import = _has_transaction_import(from_imports)
    detector = PatternDetector(has_transaction_import=has_txn_import)

    # Visit all methods and attributes in the class body
    for node in class_node.body:
        detector.visit(node)

    # Special handling: if on_commit was found, check if it's only in
    # mock/patch decorator strings (which would be safe)
    if "on_commit" in detector.found_patterns:
        # Walk the class for actual on_commit calls vs just patch strings
        actual_on_commit = False
        for node in ast.walk(class_node):
            if isinstance(node, ast.Attribute):
                if (
                    node.attr == "on_commit"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "transaction"
                ):
                    actual_on_commit = True
                    break
            elif isinstance(node, ast.Name) and node.id == "on_commit":
                # Could be a reference to the function itself
                # Check if it's inside a Call to patch()
                actual_on_commit = True
                break

        if not actual_on_commit:
            # The "on_commit" was only in string literals (patch targets)
            # This is still potentially relevant because TransactionTestCase
            # is needed for on_commit to actually fire. But if they're patching
            # the on_commit handler, the test might not need real on_commit.
            # Flag as REVIEW instead of UNSAFE.
            detector.found_patterns.discard("on_commit")
            detector.found_patterns.add("on_commit_patched")

    # Detect responses.start() in setUp method
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == "setUp":
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if (
                        isinstance(func, ast.Attribute)
                        and func.attr == "start"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "responses"
                    ):
                        detector.found_patterns.add("responses_start_in_setup")
                        break

    # Detect @override_settings(task_always_eager=True) on class
    for decorator in class_node.decorator_list:
        if isinstance(decorator, ast.Call):
            func = decorator.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name == "override_settings":
                for kw in decorator.keywords:
                    if (
                        kw.arg == "task_always_eager"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    ):
                        detector.found_patterns.add("task_always_eager")

    return detector.found_patterns


def analyze_file(file_path, src_root):
    """
    Parse a Python file and analyze all test classes that inherit from
    APITransactionTestCase.

    Returns a list of ClassResult objects.
    """
    results = []
    rel_path = os.path.relpath(file_path, src_root)

    try:
        source = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"  Warning: Could not read {rel_path}: {e}", file=sys.stderr)
        return results

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        print(f"  Warning: Syntax error in {rel_path}: {e}", file=sys.stderr)
        return results

    module_imports, from_imports = _resolve_import_aliases(tree)

    # Check if this file imports APITransactionTestCase at all
    has_atc = False
    for alias, (module, original_name) in from_imports.items():
        if module == "rest_framework" and original_name == "test":
            has_atc = True
            break
        if (
            module == "rest_framework.test"
            and original_name == "APITransactionTestCase"
        ):
            has_atc = True
            break
    if not has_atc:
        # File might still have classes inheriting from APITransactionTestCase
        # via other imports, but we only handle the known patterns
        return results

    # Find all class definitions
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Check if any base class is APITransactionTestCase
        is_direct_inherit = False

        for base in node.bases:
            if _is_api_transaction_test_case(base, from_imports):
                is_direct_inherit = True
                break

        if not is_direct_inherit:
            # Check if it inherits from something else that might be a custom
            # base class. We only flag classes whose bases are NOT standard
            # Python/Django classes and ARE defined in this file or imported
            # from a tests module.
            #
            # For the first version, skip classes that don't directly inherit
            # from APITransactionTestCase - they'll be caught when we analyze
            # their actual base class.
            continue

        # Analyze the class for patterns
        patterns = analyze_class(node, from_imports, module_imports)

        # Classify
        unsafe_found = []
        review_found = []

        for pattern in patterns:
            if pattern in UNSAFE_PATTERNS:
                unsafe_found.append(pattern)
            elif pattern in REVIEW_PATTERNS:
                review_found.append(pattern)
            elif pattern == "on_commit_patched":
                review_found.append("on_commit (patched/mocked)")

        if unsafe_found:
            classification = UNSAFE
            reasons = [f"{p}: {UNSAFE_PATTERNS[p]}" for p in sorted(unsafe_found)]
            reasons += [
                f"{p}: {REVIEW_PATTERNS.get(p, p)}" for p in sorted(review_found)
            ]
        elif review_found:
            classification = REVIEW
            reasons = [
                f"{p}: {REVIEW_PATTERNS.get(p, p)}" for p in sorted(review_found)
            ]
        else:
            classification = SAFE
            reasons = []

        results.append(
            ClassResult(
                file_path=rel_path,
                class_name=node.name,
                line_number=node.lineno,
                classification=classification,
                reasons=reasons,
            )
        )

    return results


def find_indirect_inheritors(file_path, src_root, direct_results=None):
    """
    Find classes that inherit from custom base classes (not directly from
    APITransactionTestCase) but are in files that import
    APITransactionTestCase.

    If the parent base class was already analyzed (from direct_results) and
    is SAFE, the child class is also classified based only on its own patterns.
    Only if the parent is UNSAFE/REVIEW, or unknown, do we flag as REVIEW.
    """
    results = []
    rel_path = os.path.relpath(file_path, src_root)

    try:
        source = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return results

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return results

    module_imports, from_imports = _resolve_import_aliases(tree)

    # Collect names of classes defined in this file that directly inherit
    # from APITransactionTestCase, along with their analysis results
    direct_inheritors = set()
    direct_class_results = {}  # class_name -> classification
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if _is_api_transaction_test_case(base, from_imports):
                    direct_inheritors.add(node.name)
                    break

    # Build a lookup for the parent's classification from direct_results
    if direct_results:
        for r in direct_results:
            if r.file_path == rel_path:
                direct_class_results[r.class_name] = r.classification

    if not direct_inheritors:
        return results

    # Now find classes that inherit from one of those direct inheritors
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name in direct_inheritors:
            continue

        for base in node.bases:
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                # e.g., module.ClassName
                base_name = base.attr

            if base_name and base_name in direct_inheritors:
                # This class inherits from a local base that extends
                # APITransactionTestCase
                patterns = analyze_class(node, from_imports, module_imports)

                unsafe_found = []
                review_found = []

                for pattern in patterns:
                    if pattern in UNSAFE_PATTERNS:
                        unsafe_found.append(pattern)
                    elif pattern in REVIEW_PATTERNS:
                        review_found.append(pattern)
                    elif pattern == "on_commit_patched":
                        review_found.append("on_commit (patched/mocked)")

                # Check parent class classification
                parent_classification = direct_class_results.get(base_name)

                if unsafe_found:
                    classification = UNSAFE
                    reasons = [
                        f"{p}: {UNSAFE_PATTERNS[p]}" for p in sorted(unsafe_found)
                    ]
                    reasons += [
                        f"{p}: {REVIEW_PATTERNS.get(p, p)}"
                        for p in sorted(review_found)
                    ]
                elif parent_classification == UNSAFE:
                    # Parent is unsafe, so child must also be unsafe
                    classification = UNSAFE
                    reasons = [
                        f"parent_class_unsafe: parent class {base_name} requires TransactionTestCase"
                    ]
                elif review_found or parent_classification is None:
                    # Parent not analyzed or has review issues
                    if parent_classification is None:
                        review_found.append("custom_base_class")
                    classification = REVIEW
                    reasons = [
                        f"{p}: {REVIEW_PATTERNS.get(p, p)}"
                        for p in sorted(review_found)
                    ]
                elif parent_classification in (SAFE, REVIEW):
                    # Parent is safe and this class has no issues → SAFE
                    classification = SAFE
                    reasons = []
                else:
                    classification = REVIEW
                    reasons = [
                        "custom_base_class: inherits from custom base, not directly from APITransactionTestCase"
                    ]

                results.append(
                    ClassResult(
                        file_path=rel_path,
                        class_name=node.name,
                        line_number=node.lineno,
                        classification=classification,
                        reasons=reasons,
                    )
                )
                break  # Only process each class once

    return results


def find_cross_file_inheritors(src_root, all_results=None):
    """
    Find classes that inherit from base classes imported from other modules
    (e.g., `from waldur_mastermind.support.tests.base import BaseTest`).

    These are classes that indirectly inherit from APITransactionTestCase
    through a base class defined in a different file.
    """
    results = []

    # Build lookup of already-classified classes
    classified = {}
    if all_results:
        for r in all_results:
            classified[(r.file_path, r.class_name)] = r.classification

    # First pass: identify known base classes that extend APITransactionTestCase
    # in non-test files (base.py, utils.py, etc.)
    known_bases = {}  # Maps (module_path, class_name) -> (file_path, classification)
    base_file_patterns = ["base.py", "utils.py", "helpers.py", "mixins.py", "common.py"]

    for root, dirs, files in os.walk(os.path.join(src_root, "src")):
        # Skip non-test directories
        if "tests" not in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            if fname not in base_file_patterns:
                continue

            fpath = os.path.join(root, fname)
            try:
                source = Path(fpath).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=fpath)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue

            _, from_imports = _resolve_import_aliases(tree)

            for node in ast.iter_child_nodes(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    if _is_api_transaction_test_case(base, from_imports):
                        rel_path = os.path.relpath(fpath, src_root)
                        # Build the importable module path
                        mod_path = (
                            rel_path.replace("/", ".")
                            .replace("\\", ".")
                            .replace(".py", "")
                        )
                        if mod_path.startswith("src."):
                            mod_path = mod_path[4:]
                        parent_cls = classified.get((rel_path, node.name))
                        known_bases[(mod_path, node.name)] = (rel_path, parent_cls)
                        break

    if not known_bases:
        return results

    # Second pass: find files that import from those base modules
    # and have classes inheriting from the imported base
    known_base_names = {name for (_, name) in known_bases}

    for root, dirs, files in os.walk(os.path.join(src_root, "src")):
        for fname in files:
            if not fname.endswith(".py") or not fname.startswith("test"):
                continue

            fpath = os.path.join(root, fname)
            try:
                source = Path(fpath).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=fpath)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue

            _, from_imports = _resolve_import_aliases(tree)
            module_imports_map, _ = _resolve_import_aliases(tree)

            # Check if any imports reference our known bases
            imported_bases = {}  # local alias -> (module, original_name, parent_cls)
            for alias, (module, original_name) in from_imports.items():
                if original_name in known_base_names:
                    # Verify the module matches one of our known base modules
                    for (mod_path, cls_name), (
                        base_file,
                        parent_cls,
                    ) in known_bases.items():
                        if cls_name == original_name and module.endswith(
                            mod_path.rsplit(".", 1)[0] if "." in mod_path else mod_path
                        ):
                            imported_bases[alias] = (module, original_name, parent_cls)
                            break
                    else:
                        # Check by partial module match
                        for (mod_path, cls_name), (
                            base_file,
                            parent_cls,
                        ) in known_bases.items():
                            if cls_name == original_name and (
                                mod_path.endswith(
                                    module.replace(".", "/") + "/" + original_name
                                )
                                or module in mod_path
                            ):
                                imported_bases[alias] = (
                                    module,
                                    original_name,
                                    parent_cls,
                                )
                                break

            if not imported_bases:
                continue

            rel_path = os.path.relpath(fpath, src_root)

            for node in ast.iter_child_nodes(tree):
                if not isinstance(node, ast.ClassDef):
                    continue

                for base in node.bases:
                    base_name = None
                    if isinstance(base, ast.Name):
                        base_name = base.id
                    elif isinstance(base, ast.Attribute):
                        base_name = base.attr

                    if base_name and base_name in imported_bases:
                        _, _, parent_cls = imported_bases[base_name]
                        patterns = analyze_class(node, from_imports, module_imports_map)

                        unsafe_found = []
                        review_found = []

                        for pattern in patterns:
                            if pattern in UNSAFE_PATTERNS:
                                unsafe_found.append(pattern)
                            elif pattern in REVIEW_PATTERNS:
                                review_found.append(pattern)
                            elif pattern == "on_commit_patched":
                                review_found.append("on_commit (patched/mocked)")

                        if unsafe_found:
                            classification = UNSAFE
                            reasons = [
                                f"{p}: {UNSAFE_PATTERNS[p]}"
                                for p in sorted(unsafe_found)
                            ]
                        elif parent_cls == UNSAFE:
                            classification = UNSAFE
                            reasons = [
                                f"parent_class_unsafe: parent class {base_name} requires TransactionTestCase"
                            ]
                        elif review_found or parent_cls is None:
                            if parent_cls is None:
                                review_found.append("custom_base_class")
                            classification = REVIEW
                            reasons = [
                                f"{p}: {REVIEW_PATTERNS.get(p, p)}"
                                for p in sorted(review_found)
                            ]
                        elif parent_cls in (SAFE, REVIEW):
                            classification = SAFE
                            reasons = []
                        else:
                            classification = REVIEW
                            reasons = ["custom_base_class: inherits from custom base"]

                        results.append(
                            ClassResult(
                                file_path=rel_path,
                                class_name=node.name,
                                line_number=node.lineno,
                                classification=classification,
                                reasons=reasons,
                            )
                        )
                        break

    return results


# ---------------------------------------------------------------------------
# Main analysis and reporting
# ---------------------------------------------------------------------------


def collect_test_files(src_dir):
    """Recursively find all .py files under src/ that are likely test files."""
    test_files = []
    for root, dirs, files in os.walk(src_dir):
        # Skip hidden directories and common non-test dirs
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for fname in files:
            if fname.endswith(".py"):
                fpath = os.path.join(root, fname)
                test_files.append(fpath)
    return sorted(test_files)


def run_analysis(src_root):
    """
    Main analysis entry point.

    Args:
        src_root: Path to the project root (parent of src/)

    Returns:
        List of ClassResult objects
    """
    src_dir = os.path.join(src_root, "src")
    if not os.path.isdir(src_dir):
        print(f"Error: src/ directory not found at {src_dir}", file=sys.stderr)
        sys.exit(1)

    all_files = collect_test_files(src_dir)
    print(f"Scanning {len(all_files)} Python files under src/...", file=sys.stderr)

    all_results = []
    files_with_classes = 0

    for fpath in all_files:
        # Analyze direct inheritors
        direct_results = analyze_file(fpath, src_root)
        if direct_results:
            files_with_classes += 1
            all_results.extend(direct_results)

        # Analyze indirect inheritors (same-file custom base classes)
        # Pass direct_results so we can resolve parent classification
        indirect_results = find_indirect_inheritors(
            fpath, src_root, direct_results=all_results
        )
        all_results.extend(indirect_results)

    # Analyze cross-file inheritors
    cross_file_results = find_cross_file_inheritors(src_root, all_results=all_results)

    # Deduplicate: cross-file results might overlap with direct results
    existing_keys = {(r.file_path, r.class_name) for r in all_results}
    for r in cross_file_results:
        if (r.file_path, r.class_name) not in existing_keys:
            all_results.append(r)

    print(
        f"Found {len(all_results)} test classes across {files_with_classes} files.",
        file=sys.stderr,
    )

    return all_results


def print_report(results):
    """Print the analysis report to stdout."""
    safe = [r for r in results if r.classification == SAFE]
    review = [r for r in results if r.classification == REVIEW]
    unsafe = [r for r in results if r.classification == UNSAFE]

    total = len(results)

    print()
    print("=" * 60)
    print("  APITransactionTestCase Migration Analysis")
    print("=" * 60)
    print()
    print("Summary:")
    print(f"  SAFE (can migrate):     {len(safe):>4} classes")
    print(f"  REVIEW (needs check):   {len(review):>4} classes")
    print(f"  UNSAFE (must keep):     {len(unsafe):>4} classes")
    print(f"  Total analyzed:         {total:>4} classes")
    print()

    # --- UNSAFE classes ---
    if unsafe:
        print("-" * 60)
        print("UNSAFE classes (must keep TransactionTestCase)")
        print("-" * 60)
        for r in sorted(unsafe, key=lambda x: x.full_name):
            print(f"  {r.full_name}")
            for reason in r.reasons:
                print(f"    - {reason}")
        print()

    # --- REVIEW classes ---
    if review:
        print("-" * 60)
        print("REVIEW classes (manual review needed)")
        print("-" * 60)
        for r in sorted(review, key=lambda x: x.full_name):
            print(f"  {r.full_name}")
            for reason in r.reasons:
                print(f"    - {reason}")
        print()

    # --- SAFE classes by app ---
    if safe:
        print("-" * 60)
        print("SAFE classes by app (can migrate to APITestCase)")
        print("-" * 60)

        by_app = defaultdict(list)
        for r in safe:
            by_app[r.app_name].append(r)

        for app_name in sorted(by_app.keys()):
            app_classes = sorted(by_app[app_name], key=lambda x: x.full_name)
            print(f"\n  {app_name} ({len(app_classes)} classes):")
            for r in app_classes:
                print(f"    {r.file_path}::{r.class_name}")

    # --- SAFE files summary (for bulk migration) ---
    if safe:
        print()
        print("-" * 60)
        print("SAFE files summary (files where ALL classes are SAFE)")
        print("-" * 60)

        # Group all results by file
        by_file = defaultdict(list)
        for r in results:
            by_file[r.file_path].append(r)

        fully_safe_files = []
        for fpath, file_results in sorted(by_file.items()):
            if all(r.classification == SAFE for r in file_results):
                fully_safe_files.append((fpath, len(file_results)))

        print(f"\n  {len(fully_safe_files)} files are fully safe to migrate:\n")
        for fpath, count in fully_safe_files:
            print(f"    {fpath}  ({count} class{'es' if count > 1 else ''})")

    # --- Migration instructions ---
    print()
    print("-" * 60)
    print("Migration instructions")
    print("-" * 60)
    print()
    print("  For files using `from rest_framework import test`:")
    print("    Change: class Foo(test.APITransactionTestCase):")
    print("    To:     class Foo(test.APITestCase):")
    print()
    print("  For files using `from rest_framework.test import APITransactionTestCase`:")
    print("    Change the import to: from rest_framework.test import APITestCase")
    print("    Change: class Foo(APITransactionTestCase):")
    print("    To:     class Foo(APITestCase):")
    print()
    print("  After migration, run the tests for each migrated file:")
    print("    DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local \\")
    print("      uv run pytest <file_path> -x -v")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Analyze APITransactionTestCase usage for migration safety."
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: exit 1 if SAFE class count exceeds baseline, 0 otherwise.",
    )
    parser.add_argument(
        "--baseline",
        type=int,
        default=None,
        help="Expected number of SAFE classes (use with --ci). "
        "Fails if the actual count exceeds this. "
        "If omitted, fails on any SAFE classes.",
    )
    args = parser.parse_args()

    # Determine project root: script is in scripts/, project root is parent
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)

    # Verify we're in the right place
    src_dir = os.path.join(project_root, "src")
    if not os.path.isdir(src_dir):
        print(
            f"Error: Cannot find src/ directory. Expected at {src_dir}",
            file=sys.stderr,
        )
        print(
            "Make sure you run this script from the project root or the scripts/ directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    results = run_analysis(project_root)

    if args.ci:
        safe = [r for r in results if r.classification == SAFE]
        baseline = args.baseline if args.baseline is not None else 0
        if len(safe) > baseline:
            print(f"ERROR: {len(safe)} SAFE class(es) found, baseline is {baseline}.")
            print("New APITransactionTestCase usage must be justified.\n")
            for r in sorted(safe, key=lambda x: x.full_name):
                print(f"  {r.full_name} (line {r.line_number})")
            print(
                "\nRun `python scripts/analyze_transaction_test_cases.py` for details."
            )
            sys.exit(1)
        else:
            print(f"OK: {len(safe)} SAFE class(es) found (baseline: {baseline}).")
            sys.exit(0)
    else:
        print_report(results)


if __name__ == "__main__":
    main()
