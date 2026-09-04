"""Operations used by regenerated migration squashes (``scripts/squash_migrations.py``)."""

from django.db.migrations.operations.base import Operation


class FlushDeferredSql(Operation):
    """Give the next operation what a migration boundary gave the original.

    A regenerated squash runs the data code of its originals between schema
    operations, all in one transaction. Two things the original chain got for free
    from committing between migrations have to be done by hand:

    - ``CreateModel`` queues the indexes, unique constraints and foreign-key
      constraints of a new table as deferred SQL that Django executes when the
      migration finishes. An ``AlterUniqueTogether``, ``RemoveIndex`` or
      ``AlterField`` on a foreign key later in the same squash would look for a
      constraint that has not been created yet, so the queue is flushed first.
    - On PostgreSQL, rows a data operation touches leave the checks of deferred
      foreign keys queued until commit, and any ``ALTER TABLE`` on a table involved
      then fails with "cannot ALTER TABLE ... because it has pending trigger
      events". Checking the constraints now (``SET CONSTRAINTS ALL IMMEDIATE``)
      drains that queue, like the original's commit did; deferring them again
      keeps later data operations as free as they were.

    The regeneration emits one of these before and after every kept data
    operation.
    """

    reversible = True

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE", params=None)
            schema_editor.execute("SET CONSTRAINTS ALL DEFERRED", params=None)
        while schema_editor.deferred_sql:
            schema_editor.execute(schema_editor.deferred_sql.pop(0))

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def describe(self):
        return "Check deferred constraints and run deferred DDL of the tables created so far"

    @property
    def migration_name_fragment(self):
        return "flush_deferred_sql"
