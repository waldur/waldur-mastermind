"""Operations used by regenerated migration squashes (``scripts/squash_migrations.py``)."""

from django.db.migrations.operations.base import Operation


class FlushDeferredSql(Operation):
    """Run the schema editor's deferred DDL now instead of at the end of the migration.

    ``CreateModel`` queues the indexes, unique constraints and foreign-key
    constraints of a new table as deferred SQL that Django executes when the
    migration finishes. A regenerated squash creates a model and may alter it later
    in the *same* migration, at the point in history where the original did; an
    ``AlterUniqueTogether``, ``RemoveIndex`` or ``AlterField`` on a foreign key would
    then look for a constraint that has not been created yet. The squash therefore
    flushes the queue before each run of schema operations, which is exactly what
    the original chain got from migration boundaries.
    """

    reversible = True

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        while schema_editor.deferred_sql:
            schema_editor.execute(schema_editor.deferred_sql.pop(0))

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def describe(self):
        return "Run deferred DDL of the tables created so far"

    @property
    def migration_name_fragment(self):
        return "flush_deferred_sql"
