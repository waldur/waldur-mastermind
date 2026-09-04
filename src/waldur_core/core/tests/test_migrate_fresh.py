import re
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, override_settings

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


# pytest runs with --no-migrations, which empties MIGRATION_MODULES and with it the
# loader; these tests read the migration files, so they restore the default modules.
load_migrations = override_settings(MIGRATION_MODULES={})


@load_migrations
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
        self.assertTrue(loader.replacements)
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


@load_migrations
class SquashesKeepDdlTest(TestCase):
    """DDL-only RunSQL is the one thing ``migrate_fresh`` replays from the squash.

    A fresh database built by ``migrate_fresh`` never runs RunPython, so DDL that an
    original issued from Python must exist as RunSQL in its squash (hand-added and
    kept by the regeneration), or it silently disappears on new installs.
    """

    def test_every_ddl_run_sql_of_replaced_originals_is_in_the_squash(self):
        loader = MigrationLoader(None, replace_migrations=False)
        self.assertTrue(loader.replacements)
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


def _originals_graph():
    """The graph of original migrations, as ``scripts/squash_migrations.py`` builds it:
    squash nodes removed, their children remapped to the originals."""
    loader = MigrationLoader(None, replace_migrations=False)
    graph = loader.graph
    for key, migration in loader.replacements.items():
        if key in graph.nodes:
            graph.remove_replacement_node(key, migration.replaces)
    return loader, graph


def _applied_with_range(dep, ancestors, loader, graph):
    """Whether ``dep`` is applied on every database that has the range applied.

    Same rule as ``_applied_with_range`` in ``scripts/squash_migrations.py``.
    """
    app, name = dep
    if name == "__first__":
        return any(a == app for a, _ in ancestors)
    if dep in loader.replacements:  # names a squash: all of its originals must be
        replaced = {r for r in loader.replacements[dep].replaces if r in graph.nodes}
        return bool(replaced) and replaced <= ancestors
    return dep in ancestors


@load_migrations
class SquashDependenciesTest(TestCase):
    """Django counts a replaces-squash as applied on every database that has all of
    its originals applied, and ``check_consistent_history`` then requires each of the
    squash's dependencies to be applied as well. A squash may therefore only depend
    on what its replaced range itself depends on. Anything newer stops ``migrate`` on
    every database from before it: the squashes regenerated in 0676fed33 depended on
    ``logging.0028``, and no existing database could upgrade (#354).
    """

    def test_every_dependency_of_a_squash_is_an_ancestor_of_its_range(self):
        loader, graph = _originals_graph()
        self.assertTrue(loader.replacements)
        offending = []
        for key, squash in loader.replacements.items():
            rng = {r for r in squash.replaces if r in graph.nodes}
            if not rng:
                continue  # originals deleted: a plain migration wearing `replaces`
            ancestors = set().union(*(set(graph.forwards_plan(n)) for n in rng)) - rng
            for dep in squash.dependencies:
                if not _applied_with_range(dep, ancestors, loader, graph):
                    offending.append(f"{key[0]}.{key[1]} -> {dep[0]}.{dep[1]}")
        self.assertEqual(sorted(offending), [])
