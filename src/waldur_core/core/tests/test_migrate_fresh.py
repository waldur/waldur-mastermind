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


class SquashesKeepDdlTest(TestCase):
    """Regenerated squashes drop RunSQL; DDL-only RunSQL must be re-added by hand.

    A fresh database only ever applies the squash, so DDL present in a replaced
    original but missing from its squash would silently disappear on new installs.
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
