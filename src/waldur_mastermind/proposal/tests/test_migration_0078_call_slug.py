"""Coverage for 0078_call_slug_index_and_default.

The migration repairs databases that came through ``0036_call_slug`` (raw SQL:
a ``DEFAULT ''`` and no index on ``proposal_call.slug``). It must be a no-op on a
database whose schema was built from the models, which is what ``migrate_fresh``
- every ``initdb`` since 2026-08-29 - produces and where the index already exists.
Its first version compared a quoted index name with bare introspection names and
re-created the index on every such database (#354).
"""

from importlib import import_module

from django.apps import apps as django_apps
from django.db import connection
from django.test import TestCase

from waldur_mastermind.proposal import models

migration = import_module(
    "waldur_mastermind.proposal.migrations.0078_call_slug_index_and_default"
)


def _slug_indexes():
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(
            cursor, models.Call._meta.db_table
        )
    return {
        name
        for name, info in constraints.items()
        if info["index"] and info["columns"] == ["slug"]
    }


def _align():
    with connection.schema_editor() as schema_editor:
        migration.align_call_slug(django_apps, schema_editor)


class CallSlugMigrationTest(TestCase):
    def test_leaves_a_schema_built_from_the_models_alone(self):
        before = _slug_indexes()
        self.assertTrue(before)
        _align()
        self.assertEqual(_slug_indexes(), before)

    def test_creates_the_index_where_it_is_missing(self):
        expected = _slug_indexes()
        with connection.schema_editor() as schema_editor:
            for name in expected:
                schema_editor.execute(
                    schema_editor.sql_delete_index
                    % {"name": schema_editor.quote_name(name)}
                )
        self.assertEqual(_slug_indexes(), set())
        _align()
        self.assertEqual(_slug_indexes(), expected)
