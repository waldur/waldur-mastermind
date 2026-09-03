import re
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

from waldur_core.core.management.commands.migrate_fresh import (
    is_ddl,
    iter_sql,
)


class DdlDetectionTest(TestCase):
    def test_schema_objects_the_models_cannot_express_are_ddl(self):
        self.assertTrue(
            is_ddl(
                "CREATE INDEX log_event_org ON logging_event((context->>'customer_uuid'));"
            )
        )
        self.assertTrue(
            is_ddl("CREATE OR REPLACE FUNCTION f() RETURNS trigger AS $$ BEGIN END $$;")
        )
        self.assertTrue(
            is_ddl(
                "DROP TRIGGER IF EXISTS t ON x;\nCREATE TRIGGER t BEFORE INSERT ON x EXECUTE FUNCTION f();"
            )
        )
        self.assertTrue(is_ddl("create unique index concurrently idx on t (a);"))
        self.assertTrue(is_ddl("ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0);"))
        self.assertTrue(is_ddl("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))

    def test_data_statements_are_not_ddl(self):
        self.assertFalse(
            is_ddl(
                "SELECT setval(pg_get_serial_sequence('t', 'id'), (SELECT MAX(id) FROM t) + 1);"
            )
        )
        self.assertFalse(
            is_ddl(
                "ALTER TABLE marketplace_category DROP COLUMN IF EXISTS description_da;"
            )
        )
        self.assertFalse(is_ddl("UPDATE t SET a = 1 WHERE b IS NULL;"))
        self.assertFalse(is_ddl("DROP TABLE IF EXISTS old_table;"))

    def test_iter_sql_flattens_every_run_sql_form(self):
        self.assertEqual(
            iter_sql("CREATE INDEX a ON b (c);"), ["CREATE INDEX a ON b (c);"]
        )
        self.assertEqual(iter_sql(["one;", ("two %s;", [1])]), ["one;", "two %s;"])
        self.assertEqual(iter_sql(migrations.RunSQL.noop), [])
        self.assertEqual(iter_sql(""), [])


class MigrateFreshGuardTest(TestCase):
    def test_refuses_a_database_that_already_has_tables(self):
        with self.assertRaisesMessage(CommandError, "Database is not empty"):
            call_command("migrate_fresh", "--check", stdout=StringIO())


def _ddl_of(migration):
    return {
        re.sub(r"\s+", " ", statement).strip()
        for op in migration.operations
        if isinstance(op, migrations.RunSQL)
        for statement in iter_sql(op.sql)
        if is_ddl(statement)
    }


def _data_operations(migration):
    """Every operation of a migration that a state diff cannot regenerate.

    RunPython is compared by code object: a squash must run the *same* function as
    the original (it references it through ``_original()``), not a copy that could
    drift. RunSQL is compared statement by statement.
    """
    found = []
    for op in migration.operations:
        if isinstance(op, migrations.RunPython):
            if op.code is not migrations.RunPython.noop:
                found.append(("python", op.code, op.reverse_code))
        elif isinstance(op, migrations.RunSQL):
            for statement in iter_sql(op.sql):
                found.append(("sql", re.sub(r"\s+", " ", statement).strip()))
    return found


class SquashesKeepDataOperationsTest(TestCase):
    """A replaces-squash is applied whenever *none* of its replaced migrations is
    applied - on a database upgrading from before the range just as on an empty one
    (``MigrationLoader.build_graph``). Every backfill and cleanup of the replaced
    originals must therefore be in the squash, or upgrades skip it silently while
    ``django_migrations`` records the original as applied.
    """

    def test_every_run_python_and_run_sql_of_replaced_originals_is_in_the_squash(
        self,
    ):
        loader = MigrationLoader(None, replace_migrations=False)
        missing = []
        for key, squash in loader.replacements.items():
            present = _data_operations(squash)
            for replaced in squash.replaces:
                if replaced not in loader.disk_migrations:
                    continue
                for item in _data_operations(loader.disk_migrations[replaced]):
                    if item not in present:
                        what = (
                            item[1].__qualname__
                            if item[0] == "python"
                            else item[1][:80]
                        )
                        missing.append(f"{key[0]}.{key[1]} lacks {replaced[1]}: {what}")
        self.assertEqual(missing, [])


class SquashesKeepDdlTest(TestCase):
    """DDL-only RunSQL is the one thing ``migrate_fresh`` replays from the squash.

    A fresh database built by ``migrate_fresh`` never runs RunPython, so DDL that an
    original issued from Python must exist as RunSQL in its squash (hand-added and
    kept by the regeneration), or it silently disappears on new installs.
    """

    def test_every_ddl_run_sql_of_replaced_originals_is_in_the_squash(self):
        loader = MigrationLoader(None, replace_migrations=False)
        missing = []
        for key, squash in loader.replacements.items():
            expected = set().union(
                *(
                    _ddl_of(loader.disk_migrations[replaced])
                    for replaced in squash.replaces
                    if replaced in loader.disk_migrations
                )
            )
            for statement in expected - _ddl_of(squash):
                missing.append(f"{key[0]}.{key[1]}: {statement[:80]}")
        self.assertEqual(missing, [])
