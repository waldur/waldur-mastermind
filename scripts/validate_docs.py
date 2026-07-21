#!/usr/bin/env python3
"""
Documentation validation script for Waldur.

Checks for:
1. Broken internal markdown links
2. Invalid file path references (src/... patterns)
3. Missing Python classes/functions referenced in docs
4. Git-based staleness detection

Usage:
    python scripts/validate_docs.py [--verbose] [--fix-links] [--check-staleness]
"""

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class ValidationIssue:
    """Represents a single validation issue."""

    file: str
    line: int
    issue_type: str
    message: str
    suggestion: str | None = None

    def __str__(self):
        base = f"{self.file}:{self.line}: [{self.issue_type}] {self.message}"
        if self.suggestion:
            base += f"\n    Suggestion: {self.suggestion}"
        return base


@dataclass
class ValidationResult:
    """Aggregated validation results."""

    issues: list = field(default_factory=list)
    files_checked: int = 0
    links_checked: int = 0
    paths_checked: int = 0
    python_refs_checked: int = 0

    @property
    def has_errors(self):
        return len(self.issues) > 0

    def add_issue(self, issue: ValidationIssue):
        self.issues.append(issue)

    def summary(self):
        error_counts = defaultdict(int)
        for issue in self.issues:
            error_counts[issue.issue_type] += 1

        lines = [
            f"\n{'=' * 60}",
            "Documentation Validation Summary",
            f"{'=' * 60}",
            f"Files checked: {self.files_checked}",
            f"Links checked: {self.links_checked}",
            f"Path references checked: {self.paths_checked}",
            f"Python references checked: {self.python_refs_checked}",
            f"Total issues found: {len(self.issues)}",
        ]

        if error_counts:
            lines.append("\nIssues by type:")
            for issue_type, count in sorted(error_counts.items()):
                lines.append(f"  - {issue_type}: {count}")

        return "\n".join(lines)


class DocValidator:
    """Main documentation validator class."""

    # Patterns for extracting references from markdown
    MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    FILE_PATH_PATTERN = re.compile(r"`(src/[^`\s]+)`")
    PYTHON_IMPORT_PATTERN = re.compile(r"(?:from|import)\s+(waldur_\w+(?:\.\w+)*)")
    PYTHON_CLASS_PATTERN = re.compile(
        r"\b([A-Z][a-zA-Z0-9]+(?:Mixin|ViewSet|Serializer|Model|Filter|Backend|Manager|Executor|Processor))\b"
    )
    CODE_BLOCK_PATTERN = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)

    # Patterns to skip (external links, anchors, etc.)
    SKIP_LINK_PATTERNS = [
        re.compile(r"^https?://"),
        re.compile(r"^mailto:"),
        re.compile(r"^#"),
        re.compile(r"^data:"),
    ]

    # Known external packages (not in src/)
    EXTERNAL_PACKAGES = {
        "waldur_api_client",  # Generated API client
        "waldur_client",  # Python SDK
    }

    # Frontend file extensions (skip if this is backend-only repo)
    FRONTEND_EXTENSIONS = {".tsx", ".ts", ".jsx", ".js", ".css", ".scss"}

    # Placeholder patterns in example paths (skip these)
    PLACEHOLDER_PATTERNS = [
        re.compile(r"your[_-]?custom", re.IGNORECASE),
        re.compile(r"my[_-]?custom", re.IGNORECASE),
        re.compile(r"example[_-]?", re.IGNORECASE),
        re.compile(r"<[^>]+>"),  # <placeholder> style
    ]

    def __init__(self, docs_dir: Path, src_dir: Path, verbose: bool = False):
        self.docs_dir = docs_dir
        self.src_dir = src_dir
        self.verbose = verbose
        self.result = ValidationResult()
        self._python_symbols_cache = None
        self._has_frontend_code = None

    def _is_backend_only_repo(self) -> bool:
        """Check if this repo has no frontend code."""
        if self._has_frontend_code is None:
            # Quick check for any TypeScript/JavaScript files
            self._has_frontend_code = (
                any(self.src_dir.rglob("*.tsx"))
                or any(self.src_dir.rglob("*.ts"))
                or any(self.src_dir.rglob("*.jsx"))
            )
        return not self._has_frontend_code

    def log(self, message: str):
        """Print message if verbose mode is enabled."""
        if self.verbose:
            print(f"  {message}")

    def validate_all(self, check_staleness: bool = False) -> ValidationResult:
        """Run all validation checks."""
        md_files = list(self.docs_dir.rglob("*.md"))
        self.result.files_checked = len(md_files)

        print(f"Validating {len(md_files)} markdown files...")

        for md_file in md_files:
            self._validate_file(md_file)

        if check_staleness:
            self._check_staleness(md_files)

        return self.result

    def _validate_file(self, file_path: Path):
        """Validate a single markdown file."""
        self.log(f"Checking {file_path.relative_to(self.docs_dir)}")

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            self.result.add_issue(
                ValidationIssue(
                    file=str(file_path),
                    line=0,
                    issue_type="READ_ERROR",
                    message=f"Could not read file: {e}",
                )
            )
            return

        lines = content.split("\n")

        in_code_block = False
        for line_num, line in enumerate(lines, start=1):
            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                continue

            if not in_code_block:
                self._check_markdown_links(file_path, line_num, line)
            self._check_file_paths(file_path, line_num, line)
            self._check_python_references(file_path, line_num, line, content)

    def _check_markdown_links(self, file_path: Path, line_num: int, line: str):
        """Check internal markdown links."""
        for match in self.MARKDOWN_LINK_PATTERN.finditer(line):
            link_text, link_target = match.groups()
            self.result.links_checked += 1

            # Skip external links and anchors
            if any(p.match(link_target) for p in self.SKIP_LINK_PATTERNS):
                continue

            # Handle anchor links in same file
            if link_target.startswith("#"):
                continue

            # Split target and anchor
            if "#" in link_target:
                link_target = link_target.split("#")[0]

            # Skip empty links after anchor removal
            if not link_target:
                continue

            # Resolve relative path
            if link_target.startswith("/"):
                # Absolute path from docs root
                target_path = self.docs_dir / link_target.lstrip("/")
            else:
                # Relative path from current file
                target_path = file_path.parent / link_target

            # Normalize path
            try:
                target_path = target_path.resolve()
            except Exception:
                pass

            # Check if target exists
            if not target_path.exists():
                suggestion = self._suggest_similar_file(target_path, file_path.parent)
                self.result.add_issue(
                    ValidationIssue(
                        file=str(file_path.relative_to(self.docs_dir.parent)),
                        line=line_num,
                        issue_type="BROKEN_LINK",
                        message=f"Link target not found: {link_target}",
                        suggestion=suggestion,
                    )
                )

    def _check_file_paths(self, file_path: Path, line_num: int, line: str):
        """Check file path references (src/... patterns)."""
        for match in self.FILE_PATH_PATTERN.finditer(line):
            path_ref = match.group(1)
            self.result.paths_checked += 1

            # Remove line number suffix if present (e.g., src/file.py:36-70)
            clean_path = re.sub(r":\d+(-\d+)?$", "", path_ref)

            # Skip frontend files if this appears to be a backend-only repo
            path_ext = Path(clean_path).suffix
            if path_ext in self.FRONTEND_EXTENSIONS and self._is_backend_only_repo():
                continue  # Skip frontend path checks in backend-only repo

            # Skip placeholder/example paths
            if any(p.search(clean_path) for p in self.PLACEHOLDER_PATTERNS):
                continue

            # Check if path exists
            full_path = self.docs_dir.parent / clean_path

            if not full_path.exists():
                suggestion = self._suggest_similar_src_path(clean_path)
                self.result.add_issue(
                    ValidationIssue(
                        file=str(file_path.relative_to(self.docs_dir.parent)),
                        line=line_num,
                        issue_type="INVALID_PATH",
                        message=f"Referenced path not found: {path_ref}",
                        suggestion=suggestion,
                    )
                )

    def _check_python_references(
        self, file_path: Path, line_num: int, line: str, full_content: str
    ):
        """Check Python class/function references in code blocks."""
        # Only check lines that look like they're in code blocks or inline code
        if "`" not in line and "```" not in full_content:
            return

        # Check for import statements
        for match in self.PYTHON_IMPORT_PATTERN.finditer(line):
            module_path = match.group(1)
            self.result.python_refs_checked += 1

            # Skip known external packages
            root_package = module_path.split(".")[0]
            if root_package in self.EXTERNAL_PACKAGES:
                continue

            if not self._module_exists(module_path):
                self.result.add_issue(
                    ValidationIssue(
                        file=str(file_path.relative_to(self.docs_dir.parent)),
                        line=line_num,
                        issue_type="INVALID_IMPORT",
                        message=f"Module not found: {module_path}",
                    )
                )

    def _module_exists(self, module_path: str) -> bool:
        """Check if a Python module path exists."""
        # Convert module path to file path
        parts = module_path.split(".")

        # Try as package (directory with __init__.py)
        package_path = self.src_dir / "/".join(parts)
        if package_path.is_dir() and (package_path / "__init__.py").exists():
            return True

        # Try as module (file.py)
        module_file = self.src_dir / "/".join(parts[:-1]) / f"{parts[-1]}.py"
        if module_file.exists():
            return True

        # Try the whole thing as a file
        full_file = self.src_dir / ("/".join(parts) + ".py")
        if full_file.exists():
            return True

        return False

    def _suggest_similar_file(self, target_path: Path, search_dir: Path) -> str | None:
        """Suggest similar files that might be the intended target."""
        target_name = target_path.name.lower()

        # Search in the same directory and docs directory
        search_dirs = [search_dir, self.docs_dir]

        for sdir in search_dirs:
            if not sdir.exists():
                continue
            for candidate in sdir.rglob("*.md"):
                if candidate.name.lower() == target_name:
                    rel_path = candidate.relative_to(sdir)
                    return f"Did you mean: {rel_path}?"

        return None

    def _suggest_similar_src_path(self, path_ref: str) -> str | None:
        """Suggest similar source paths."""
        target_name = Path(path_ref).name

        # Search for similar files
        for candidate in self.src_dir.rglob("*"):
            if candidate.name == target_name and candidate.is_file():
                rel_path = candidate.relative_to(self.docs_dir.parent)
                return f"Did you mean: {rel_path}?"

        return None

    def _check_staleness(self, md_files: list):
        """Check for potentially stale documentation using git history."""
        print("\nChecking documentation freshness...")

        stale_threshold = timedelta(days=180)  # 6 months
        now = datetime.now()

        for md_file in md_files:
            try:
                # Get last modification date from git
                result = subprocess.run(
                    ["git", "log", "-1", "--format=%ct", str(md_file)],
                    capture_output=True,
                    text=True,
                    cwd=self.docs_dir.parent,
                )

                if result.returncode == 0 and result.stdout.strip():
                    timestamp = int(result.stdout.strip())
                    last_modified = datetime.fromtimestamp(timestamp)
                    age = now - last_modified

                    if age > stale_threshold:
                        self.result.add_issue(
                            ValidationIssue(
                                file=str(md_file.relative_to(self.docs_dir.parent)),
                                line=0,
                                issue_type="STALE_DOC",
                                message=f"Document not updated in {age.days} days (last: {last_modified.strftime('%Y-%m-%d')})",
                                suggestion="Consider reviewing for accuracy",
                            )
                        )
            except Exception as e:
                self.log(f"Could not check staleness for {md_file}: {e}")


class SettingsValidator:
    """Validates WALDUR_* settings references in documentation."""

    SETTINGS_PATTERN = re.compile(r"\bWALDUR_[A-Z_]+\b")

    # Known valid prefixes that aren't settings
    KNOWN_PREFIXES = {
        "WALDUR_MASTERMIND",  # Package name
        "WALDUR_HOMEPORT",  # Frontend package
        "WALDUR_API",  # API reference
    }

    def __init__(self, docs_dir: Path, src_dir: Path):
        self.docs_dir = docs_dir
        self.src_dir = src_dir
        self._valid_settings = None

    def _load_valid_settings(self) -> set:
        """Extract valid WALDUR_* settings from codebase."""
        if self._valid_settings is not None:
            return self._valid_settings

        settings = set()

        # Search for WALDUR_ definitions in Python files
        for py_file in self.src_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
                # Find WALDUR_ assignments
                for match in re.finditer(
                    r"['\"]?(WALDUR_[A-Z_]+)['\"]?\s*[=:]", content
                ):
                    settings.add(match.group(1))
                # Find WALDUR_ in getattr/get calls
                for match in re.finditer(r"['\"]WALDUR_[A-Z_]+['\"]", content):
                    settings.add(match.group(0).strip("'\""))
            except Exception:
                continue

        self._valid_settings = settings
        return settings

    def validate(self, result: ValidationResult):
        """Check WALDUR_* settings references in docs."""
        valid_settings = self._load_valid_settings()

        print(f"\nFound {len(valid_settings)} WALDUR_* settings in codebase")

        for md_file in self.docs_dir.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                lines = content.split("\n")

                for line_num, line in enumerate(lines, start=1):
                    for match in self.SETTINGS_PATTERN.finditer(line):
                        setting = match.group(0)

                        # Skip if it's a valid setting
                        if setting in valid_settings:
                            continue

                        # Skip common false positives and known prefixes
                        if setting in {"WALDUR_AUTH", "WALDUR_CORE", "WALDUR_"}:
                            continue
                        if setting in self.KNOWN_PREFIXES:
                            continue

                        result.add_issue(
                            ValidationIssue(
                                file=str(md_file.relative_to(self.docs_dir.parent)),
                                line=line_num,
                                issue_type="UNKNOWN_SETTING",
                                message=f"Unknown setting referenced: {setting}",
                                suggestion="Check if this setting still exists or was renamed",
                            )
                        )
            except Exception:
                continue


def main():
    parser = argparse.ArgumentParser(
        description="Validate Waldur documentation for broken links and outdated references"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose output"
    )
    parser.add_argument(
        "--check-staleness",
        action="store_true",
        help="Check for stale documentation (not updated in 6+ months)",
    )
    parser.add_argument(
        "--check-settings",
        action="store_true",
        help="Validate WALDUR_* settings references",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=None,
        help="Path to docs directory (default: auto-detect)",
    )
    parser.add_argument(
        "--src-dir",
        type=Path,
        default=None,
        help="Path to src directory (default: auto-detect)",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Exit with 0 even if issues found (useful for CI warnings)",
    )

    args = parser.parse_args()

    # Auto-detect directories
    script_dir = Path(__file__).parent.parent
    docs_dir = args.docs_dir or script_dir / "docs"
    src_dir = args.src_dir or script_dir / "src"

    if not docs_dir.exists():
        print(f"Error: docs directory not found: {docs_dir}")
        sys.exit(1)

    if not src_dir.exists():
        print(f"Error: src directory not found: {src_dir}")
        sys.exit(1)

    # Run validation
    validator = DocValidator(docs_dir, src_dir, verbose=args.verbose)
    result = validator.validate_all(check_staleness=args.check_staleness)

    # Optionally check settings
    if args.check_settings:
        settings_validator = SettingsValidator(docs_dir, src_dir)
        settings_validator.validate(result)

    # Print issues
    if result.issues:
        print(f"\n{'=' * 60}")
        print("Issues Found:")
        print(f"{'=' * 60}\n")

        # Group by file
        issues_by_file = defaultdict(list)
        for issue in result.issues:
            issues_by_file[issue.file].append(issue)

        for file_path, issues in sorted(issues_by_file.items()):
            print(f"\n{file_path}:")
            for issue in sorted(issues, key=lambda x: x.line):
                print(f"  Line {issue.line}: [{issue.issue_type}] {issue.message}")
                if issue.suggestion:
                    print(f"    -> {issue.suggestion}")

    # Print summary
    print(result.summary())

    # Exit code
    if result.has_errors and not args.exit_zero:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
