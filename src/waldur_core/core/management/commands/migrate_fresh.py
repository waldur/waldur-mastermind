"""Initialize an EMPTY database from the models instead of replaying migrations.

A from-scratch ``migrate`` spends minutes re-rendering migration state for every
operation in the chain, while the resulting schema is - by construction, enforced by
``makemigrations --check`` in CI - identical to what the models describe. For a
database that contains no tables yet, this command therefore, in one transaction:

1. creates every table of every migrated app straight from the models
   (the same ``sync_apps`` path ``migrate --run-syncdb`` uses for unmigrated apps);
2. re-applies the few ``RunSQL`` operations that carry DDL the models cannot express
   (expression indexes, triggers, functions) - data-only RunSQL/RunPython are skipped,
   an empty database has nothing to backfill;
3. records every migration as applied and emits ``post_migrate`` with the real model
   registry, so content types, auth permissions and the default site are created
   exactly as after ``migrate``.

Seed data that migrations used to create (roles, notifications, features) is loaded
on every deployment path by ``import_roles`` / ``load_notifications`` /
``load_features`` (see ``docker/rootfs/usr/local/bin/initdb`` and the install guide).

The command refuses to run on a database that already has ``django_migrations`` or
any model table; ``--check`` only tests that condition (exit 0 = empty).
"""

import re

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.core.management.commands.migrate import Command as MigrateCommand
from django.core.management.sql import emit_post_migrate_signal, emit_pre_migrate_signal
from django.db import DEFAULT_DB_ALIAS, connections, router, transaction
from django.db.migrations import RunSQL
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

# Schema objects a migration may create with raw SQL and that the models cannot
# express. Anything the models *can* express (tables, columns, plain indexes) is
# already covered by sync_apps; data statements (UPDATE, setval, DROP COLUMN cleanup)
# are meaningless on an empty database.
DDL_RE = re.compile(
    r"\b(CREATE\s+(OR\s+REPLACE\s+)?(UNIQUE\s+)?"
    r"(INDEX|TRIGGER|FUNCTION|PROCEDURE|EXTENSION|TYPE|SEQUENCE|COLLATION|POLICY|SCHEMA|(MATERIALIZED\s+)?VIEW)"
    r"|ALTER\s+TABLE\s+\S+\s+ADD\s+(CONSTRAINT|COLUMN))\b",
    re.IGNORECASE,
)


def is_ddl(sql: str) -> bool:
    """True for RunSQL text that creates a schema object the models cannot express."""
    return bool(DDL_RE.search(sql))


def iter_sql(sql) -> list[str]:
    """Flatten RunSQL.sql (str | list of str | list of (str, params)) into statements."""
    if not sql or sql is RunSQL.noop:
        return []
    if isinstance(sql, str):
        return [sql]
    statements = []
    for item in sql:
        if isinstance(item, (list, tuple)):
            item = item[0]
        if item and item is not RunSQL.noop:
            statements.append(item)
    return statements


def ddl_statements(plan) -> list[tuple[str, str]]:
    """(migration label, statement) for every DDL-only RunSQL statement in a plan."""
    found = []
    for migration, _backwards in plan:
        for operation in migration.operations:
            if isinstance(operation, RunSQL):
                for statement in iter_sql(operation.sql):
                    if is_ddl(statement):
                        found.append(
                            (f"{migration.app_label}.{migration.name}", statement)
                        )
    return found


def occupied_tables(connection, alias: str) -> list[str]:
    """Model tables (or django_migrations) that already exist in the database."""
    model_tables = {
        model._meta.db_table
        for app_config in apps.get_app_configs()
        for model in router.get_migratable_models(
            app_config, alias, include_auto_created=True
        )
    }
    existing = set(connection.introspection.table_names())
    return sorted(existing & (model_tables | {"django_migrations"}))


class Command(BaseCommand):
    help = "Create the schema of an empty database from the models and record all migrations as applied."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Only report whether the database is empty (exit 0) or not (exit 1).",
        )
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="Database alias to initialize (default: %(default)s).",
        )

    def handle(self, *args, **options):
        alias = options["database"]
        connection = connections[alias]
        occupied = occupied_tables(connection, alias)
        if occupied:
            raise CommandError(
                "Database is not empty (found %s%s); use `waldur migrate` instead."
                % (", ".join(occupied[:3]), "..." if len(occupied) > 3 else "")
            )
        if options["check"]:
            self.stdout.write("Database is empty: migrate_fresh can be used.")
            return

        loader = MigrationLoader(connection)
        migrated_apps = sorted(loader.migrated_apps)
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(loader.graph.leaf_nodes())

        # Everything below is one transaction: either the database ends up fully
        # initialized and recorded, or it stays empty and initdb can simply retry.
        with transaction.atomic(using=alias):
            emit_pre_migrate_signal(
                0, False, alias, stdout=self.stdout, apps=apps, plan=plan
            )

            self.stdout.write(
                "Creating tables from models for %d apps..." % len(migrated_apps)
            )
            sync = MigrateCommand(stdout=self.stdout, stderr=self.stderr)
            sync.verbosity = 0
            sync.sync_apps(connection, migrated_apps)

            self.stdout.write("Applying DDL-only RunSQL operations...")
            with connection.cursor() as cursor:
                for label, statement in ddl_statements(plan):
                    self.stdout.write("  %s" % label)
                    cursor.execute(statement)

            self.stdout.write("Recording %d migrations as applied..." % len(plan))
            executor.recorder.ensure_schema()
            for migration, _backwards in plan:
                # Records the squash and, for replacement squashes, every replaced name.
                executor.record_migration(migration.app_label, migration.name)

            # The real registry, not a migration state: makes contenttypes/auth/sites
            # receivers create the rows they create after a regular migrate.
            emit_post_migrate_signal(
                0, False, alias, stdout=self.stdout, apps=apps, plan=plan
            )

        self.stdout.write(self.style.SUCCESS("Fresh database initialized from models."))
