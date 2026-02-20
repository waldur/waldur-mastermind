"""
Tests for migration integrity.

Catches issues like:
- Broken dependency chains
- Conflicting migration names within an app
- Reusing migration numbers from previously deleted apps
- Missing migrations for model changes
"""

import subprocess

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase


class MigrationGraphIntegrityTest(TestCase):
    def test_no_conflicts_in_migration_graph(self):
        """Ensure no two migrations in the same app share a number."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        conflicts = loader.detect_conflicts()
        self.assertEqual(
            conflicts,
            {},
            f"Migration conflicts detected: {conflicts}",
        )

    def test_all_dependencies_exist(self):
        """Ensure every migration dependency points to an existing migration."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for node_key, node in loader.graph.node_map.items():
            for parent in node.parents:
                self.assertIn(
                    parent.key,
                    loader.graph.node_map,
                    f"Migration {node_key} depends on {parent.key} which does not exist",
                )

    def test_no_missing_migrations(self):
        """Ensure all model changes have corresponding migrations."""
        try:
            call_command("makemigrations", "--check", "--dry-run", verbosity=0)
        except SystemExit as e:
            if e.code:
                self.fail(
                    "There are model changes without migrations. "
                    "Run 'makemigrations' to create them."
                )


class DeletedMigrationReuseTest(TestCase):
    """Detect when a new migration reuses a (app, name) that was previously deleted.

    This catches the exact scenario that caused InconsistentMigrationHistory in
    production: the old waldur_keycloak app was deleted, then a new app with the
    same name reused 0001_initial — but production databases still had the old
    0001_initial recorded in django_migrations.
    """

    def test_no_reuse_of_deleted_migration_names(self):
        """Current migrations must not reuse names from previously deleted migrations."""
        try:
            # Find all migration files ever deleted in git history.
            # --diff-filter=D lists only deleted files.
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--all",
                    "--diff-filter=D",
                    "--name-only",
                    "--pretty=format:",
                    "--",
                    "src/*/migrations/[0-9]*.py",
                    "src/*/*/migrations/[0-9]*.py",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.skipTest("git not available or too slow")

        if result.returncode != 0:
            self.skipTest("git command failed")

        deleted_paths = {
            line.strip() for line in result.stdout.splitlines() if line.strip()
        }
        if not deleted_paths:
            return  # Nothing was ever deleted

        # Get current migration files
        loader = MigrationLoader(None, ignore_no_migrations=True)
        current_identities = set()
        for app_label, migration_name in loader.graph.node_map:
            current_identities.add((app_label, migration_name))

        # Parse deleted paths into (app_label, migration_name) pairs
        reused = []
        for path in deleted_paths:
            # e.g. src/waldur_keycloak/migrations/0001_initial.py
            parts = path.replace("src/", "").split("/")
            if len(parts) >= 3 and parts[-2] == "migrations":
                app_label = parts[-3]
                migration_name = parts[-1].removesuffix(".py")
                if (app_label, migration_name) in current_identities:
                    reused.append(f"{app_label}.{migration_name}")

        # The placeholder 0001_initial we intentionally created is expected —
        # exclude any entries listed in KNOWN_REUSED_MIGRATIONS.
        known_reused = {
            # Placeholder matching the old waldur_keycloak app's 0001_initial
            "waldur_keycloak.0001_initial",
        }
        unexpected = sorted(set(reused) - known_reused)

        self.assertEqual(
            unexpected,
            [],
            f"These migrations reuse names from previously deleted migrations, "
            f"which will cause InconsistentMigrationHistory on databases that had "
            f"the old migrations applied: {unexpected}. "
            f"Use a higher migration number instead.",
        )
