#!/usr/bin/env python3
"""
Analyze and squash Django migrations to speed up fresh database setup.

This script:
1. Loads Django's migration graph
2. Identifies apps with many migrations
3. Finds safe squash ranges (no circular cross-app dependencies)
4. Optionally runs squashmigrations and post-processes the output

Usage:
    # Analyze only (dry run) - shows what could be squashed
    uv run python scripts/squash_migrations.py

    # Analyze with lower threshold
    uv run python scripts/squash_migrations.py --min-migrations 10

    # Actually squash a specific app
    uv run python scripts/squash_migrations.py --squash <app_label>

    # Squash all apps above threshold
    uv run python scripts/squash_migrations.py --squash-all

    # Post-process an existing squash file (fix RunPython, imports)
    uv run python scripts/squash_migrations.py --fix <path_to_squash_file>

Environment:
    DJANGO_SETTINGS_MODULE must be set (defaults to waldur_core.server.test_settings)
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def setup_django():
    """Initialize Django settings."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "waldur_core.server.test_settings")
    import django

    django.setup()


def get_migration_graph():
    """Load and return Django's migration graph."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    return loader.graph, loader


def get_app_migrations(graph, app_label):
    """Get ordered list of migration names for an app."""
    migrations = sorted(name for app, name in graph.nodes.keys() if app == app_label)
    return migrations


def get_cross_app_deps(graph, app_label):
    """Find all cross-app dependencies involving this app.

    Returns:
        incoming: dict of {(other_app, other_migration): (app_label, migration)}
            - migrations in OTHER apps that depend on this app's migrations
        outgoing: dict of {(app_label, migration): (other_app, other_migration)}
            - this app's migrations that depend on OTHER apps
    """
    incoming = []  # other apps depending on this app
    outgoing = []  # this app depending on other apps

    for node_key in graph.nodes.keys():
        node = graph.node_map[node_key]
        app, name = node_key

        if app == app_label:
            # Check what this migration depends on (parents) in other apps
            for parent in node.parents:
                parent_key = parent.key
                if parent_key[0] != app_label:
                    outgoing.append((node_key, parent_key))
        else:
            # Check if this migration depends on our app
            for parent in node.parents:
                parent_key = parent.key
                if parent_key[0] == app_label:
                    incoming.append((node_key, parent_key))

    return incoming, outgoing


def find_safe_ranges(graph, app_label, min_range_size=5):
    """Find migration ranges that can be safely squashed.

    A range is safe if squashing it doesn't create circular dependencies.
    A circular dependency occurs when:
    - Migration X (inside range) is depended on by app B's migration Y
    - App B's migration Z (where Z <= Y) is depended on by migration W (inside range)
    This means the squash would depend on B and B would depend on the squash = cycle.

    Returns list of (start, end) migration name tuples.
    """
    migrations = get_app_migrations(graph, app_label)
    if not migrations:
        return []

    incoming, outgoing = get_cross_app_deps(graph, app_label)

    # Build sets for quick lookup
    # Which of our migrations are depended on by which external apps?
    depended_on_by = defaultdict(set)  # our_migration -> set of (app, migration)
    for (ext_app, ext_mig), (_, our_mig) in incoming:
        depended_on_by[our_mig].add((ext_app, ext_mig))

    # Which external apps do our migrations depend on?
    depends_on = defaultdict(set)  # our_migration -> set of (app, migration)
    for (_, our_mig), (ext_app, ext_mig) in outgoing:
        depends_on[our_mig].add((ext_app, ext_mig))

    # Find migrations that are "boundary" points - can't be inside a squash range
    # because they'd create circular deps
    #
    # A migration M is unsafe to include in a range if:
    # - M depends on external app X, AND
    # - Some other migration in the range is depended on by app X
    # This creates: squash -> X -> squash (cycle)

    # Strategy: find maximal contiguous ranges where no circular dep exists
    # We'll use a greedy approach: extend ranges and check for cycles

    def check_range_safe(start_idx, end_idx):
        """Check if squashing migrations[start_idx:end_idx+1] would create a cycle."""
        range_migrations = set(migrations[start_idx : end_idx + 1])

        # Collect all external apps this range depends on
        range_depends_on_apps = set()
        for mig in range_migrations:
            for ext_app, _ in depends_on.get(mig, set()):
                range_depends_on_apps.add(ext_app)

        # Collect all external apps that depend on migrations in this range
        range_depended_by_apps = set()
        for mig in range_migrations:
            for ext_app, _ in depended_on_by.get(mig, set()):
                range_depended_by_apps.add(ext_app)

        # If any app appears in both sets, we have a potential cycle
        circular_apps = range_depends_on_apps & range_depended_by_apps
        if not circular_apps:
            return True

        # More precise check: for each circular app, verify the actual cycle
        for ext_app in circular_apps:
            # Find the external migrations that depend on our range
            ext_deps_on_us = set()
            for mig in range_migrations:
                for ea, em in depended_on_by.get(mig, set()):
                    if ea == ext_app:
                        ext_deps_on_us.add(em)

            # Find the external migrations our range depends on
            we_dep_on_ext = set()
            for mig in range_migrations:
                for ea, em in depends_on.get(mig, set()):
                    if ea == ext_app:
                        we_dep_on_ext.add(em)

            # Check if there's actually a path: if any ext migration we depend on
            # comes before (or is the same as) any ext migration that depends on us,
            # then the cycle is real.
            # In migration ordering, if we depend on ext.A and ext.B depends on us,
            # and A <= B in ext's migration order, that's a cycle through the squash.
            # Actually, the squash replaces all migrations in the range with one,
            # so the squash would depend on ext.A and ext.B would depend on squash.
            # That's only a cycle if ext.A depends on ext.B (directly or transitively),
            # but since A <= B in ext's order, B depends on A, not vice versa.
            # Wait - the issue is simpler: the squash node depends on ext.A,
            # and ext.B depends on the squash node. This is NOT a cycle unless
            # ext.A depends on ext.B, which it doesn't since A comes before B.
            # Actually, it IS a cycle if B comes before A in ext's ordering,
            # because then we'd have: squash -> ext.A -> ... -> ext.B -> squash
            # But actually Django checks for this differently.
            #
            # The real check: if the squash depends on ext_app AND ext_app depends
            # on the squash, that's a cycle in the app-level dependency graph.
            # Django doesn't allow this.
            return False

        return True

    # Find safe ranges using a greedy approach
    safe_ranges = []
    i = 0
    while i < len(migrations):
        # Try to extend a range starting at i
        best_end = i
        for j in range(i + 1, len(migrations)):
            if check_range_safe(i, j):
                best_end = j
            else:
                break

        range_size = best_end - i + 1
        if range_size >= min_range_size:
            safe_ranges.append((migrations[i], migrations[best_end]))

        # Move past this range
        i = best_end + 1

    return safe_ranges


def find_existing_squashes(graph, app_label):
    """Find migrations that already have a 'replaces' list (existing squashes)."""
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    squashes = []
    for (app, name), migration in loader.disk_migrations.items():
        if app == app_label and hasattr(migration, "replaces") and migration.replaces:
            squashes.append((name, migration.replaces))
    return squashes


def analyze_all_apps(graph, min_migrations=15):
    """Analyze all apps and report squash opportunities."""
    app_counts = defaultdict(int)
    for app, _ in graph.nodes.keys():
        app_counts[app] += 1

    results = []
    for app, count in sorted(app_counts.items(), key=lambda x: -x[1]):
        if count < min_migrations:
            continue

        safe_ranges = find_safe_ranges(graph, app)
        existing = find_existing_squashes(graph, app)
        squashable = sum(
            len(
                get_app_migrations(graph, app)[
                    get_app_migrations(graph, app).index(start) : get_app_migrations(
                        graph, app
                    ).index(end)
                    + 1
                ]
            )
            for start, end in safe_ranges
        )

        results.append(
            {
                "app": app,
                "total": count,
                "safe_ranges": safe_ranges,
                "existing_squashes": existing,
                "squashable": squashable,
                "reduction": squashable - len(safe_ranges) if safe_ranges else 0,
            }
        )

    return results


def fix_squash_file(filepath):
    """Post-process a squashed migration file.

    - Replace all RunPython operations with noop
    - Remove unused imports
    """
    filepath = Path(filepath)
    if not filepath.exists():
        print(f"Error: {filepath} does not exist")
        return False

    content = filepath.read_text()
    original = content

    # Replace RunPython blocks with noop
    # Handle both single-line and multi-line RunPython calls
    # Match RunPython(...) including nested parentheses
    def replace_runpython(match):
        return (
            "migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop)"
        )

    # Pattern to match migrations.RunPython(...) with nested parens
    # Uses a loop to handle arbitrary nesting depth
    changed = True
    while changed:
        new_content = re.sub(
            r"migrations\.RunPython\((?:[^()]*|\((?:[^()]*|\([^()]*\))*\))*\)",
            replace_runpython,
            content,
            flags=re.DOTALL,
        )
        changed = new_content != content
        content = new_content

    # Remove imports that were only used by RunPython functions
    # Keep standard Django imports and validator/model imports
    lines = content.split("\n")
    cleaned_lines = []
    for line in lines:
        # Skip import lines that reference specific migration modules
        # e.g., "import waldur_mastermind.marketplace.migrations.0134_..."
        if re.match(r"\s*import\s+\w+\.\w+\.migrations\.\d+", line):
            continue
        # Skip "from ... migrations.NNNN import ..."
        if re.match(r"\s*from\s+\w+.*\.migrations\.\d+", line):
            continue
        cleaned_lines.append(line)

    content = "\n".join(cleaned_lines)

    if content != original:
        filepath.write_text(content)
        print(f"Fixed: {filepath}")
        print("  - Replaced RunPython operations with noop")
        print("  - Removed migration-specific imports")
        return True
    else:
        print(f"No changes needed: {filepath}")
        return False


def find_migration_dir(app_label):
    """Find the migrations directory for a given app label."""
    search_dirs = [
        Path("src/waldur_core"),
        Path("src/waldur_mastermind"),
        Path("src"),
    ]

    for base in search_dirs:
        # Direct match (e.g., waldur_openstack)
        candidate = base / app_label / "migrations"
        if candidate.is_dir():
            return candidate

        # Check subdirectories
        for child in base.iterdir():
            if child.is_dir():
                candidate = child / "migrations"
                if candidate.is_dir() and child.name == app_label:
                    return candidate

    # Try Django's app registry
    from django.apps import apps

    try:
        app_config = apps.get_app_config(app_label)
        mig_dir = Path(app_config.path) / "migrations"
        if mig_dir.is_dir():
            return mig_dir
    except LookupError:
        pass

    return None


def run_squash(app_label, start, end):
    """Run squashmigrations and post-process the result."""
    settings = os.environ.get(
        "DJANGO_SETTINGS_MODULE", "waldur_core.server.test_settings"
    )

    print(f"\nSquashing {app_label}: {start} -> {end}")

    # Run Django's squashmigrations
    cmd = [
        sys.executable,
        "src/manage.py",
        "squashmigrations",
        "--settings",
        settings,
        app_label,
        start,
        end,
        "--no-input",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("Error running squashmigrations:")
        print(result.stderr)
        return None

    print(result.stdout)

    # Find the generated squash file
    mig_dir = find_migration_dir(app_label)
    if not mig_dir:
        print(f"Could not find migrations directory for {app_label}")
        return None

    # Look for the squash file (most recently modified .py file with "squashed" in name)
    squash_files = sorted(
        mig_dir.glob("*squashed*.py"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not squash_files:
        print("Could not find generated squash file")
        return None

    squash_file = squash_files[0]
    print(f"Generated: {squash_file}")

    # Post-process
    fix_squash_file(squash_file)

    return squash_file


def print_analysis(results):
    """Print a formatted analysis report."""
    total_current = sum(r["total"] for r in results)
    total_reduction = sum(r["reduction"] for r in results)

    print("\n" + "=" * 70)
    print("MIGRATION SQUASH ANALYSIS")
    print("=" * 70)
    print("\nApps with significant migration counts:\n")
    print(f"{'App':<30} {'Total':>6} {'Squashable':>10} {'Reduction':>10}")
    print("-" * 60)

    for r in results:
        print(
            f"{r['app']:<30} {r['total']:>6} {r['squashable']:>10} {r['reduction']:>10}"
        )
        if r["safe_ranges"]:
            for start, end in r["safe_ranges"]:
                migrations = get_app_migrations(get_migration_graph()[0], r["app"])
                start_idx = migrations.index(start)
                end_idx = migrations.index(end)
                count = end_idx - start_idx + 1
                print(f"  Range: {start} -> {end} ({count} migrations)")
        if r["existing_squashes"]:
            for name, replaces in r["existing_squashes"]:
                print(f"  Existing squash: {name} (replaces {len(replaces)})")

    print("-" * 60)
    print(f"{'TOTAL':<30} {total_current:>6} {'':>10} {total_reduction:>10}")
    print(
        f"\nPotential reduction: {total_current} -> {total_current - total_reduction} migrations"
    )
    print(
        f"({total_reduction} fewer, {total_reduction / total_current * 100:.0f}% reduction)"
    )


def main():
    parser = argparse.ArgumentParser(description="Analyze and squash Django migrations")
    parser.add_argument(
        "--min-migrations",
        type=int,
        default=15,
        help="Minimum migration count to consider an app (default: 15)",
    )
    parser.add_argument(
        "--squash",
        metavar="APP_LABEL",
        help="Squash migrations for a specific app",
    )
    parser.add_argument(
        "--squash-all",
        action="store_true",
        help="Squash all apps above threshold",
    )
    parser.add_argument(
        "--fix",
        metavar="FILE",
        help="Post-process an existing squash file (fix RunPython, imports)",
    )
    parser.add_argument(
        "--min-range",
        type=int,
        default=5,
        help="Minimum range size to consider for squashing (default: 5)",
    )
    args = parser.parse_args()

    # Handle --fix without Django setup
    if args.fix:
        fix_squash_file(args.fix)
        return

    setup_django()
    graph, loader = get_migration_graph()

    if args.squash:
        # Squash a specific app
        safe_ranges = find_safe_ranges(graph, args.squash, args.min_range)
        if not safe_ranges:
            print(f"No safe squash ranges found for {args.squash}")
            return

        print(f"Safe ranges for {args.squash}:")
        for start, end in safe_ranges:
            migrations = get_app_migrations(graph, args.squash)
            count = migrations.index(end) - migrations.index(start) + 1
            print(f"  {start} -> {end} ({count} migrations)")

        for start, end in safe_ranges:
            squash_file = run_squash(args.squash, start, end)
            if squash_file:
                print(f"Created: {squash_file}")

    elif args.squash_all:
        results = analyze_all_apps(graph, args.min_migrations)
        for r in results:
            if not r["safe_ranges"]:
                continue
            for start, end in r["safe_ranges"]:
                squash_file = run_squash(r["app"], start, end)
                if squash_file:
                    print(f"Created: {squash_file}")

    else:
        # Analysis mode (default)
        results = analyze_all_apps(graph, args.min_migrations)
        print_analysis(results)


if __name__ == "__main__":
    main()
