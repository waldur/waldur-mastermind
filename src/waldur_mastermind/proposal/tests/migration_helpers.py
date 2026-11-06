"""Run a proposal data migration against the schema it was written for.

pytest builds the test database from the current models, and the models move
on: a data migration's function only runs against the historical state it
was written for. These helpers migrate the test database back to that state
inside the test's transaction, and forward again afterwards. The test class
needs ``@override_settings(MIGRATION_MODULES={})``, because --no-migrations
empties the migration loader.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder


def _mark_everything_applied():
    """Record the test database as fully migrated, which it is.

    With --no-migrations nothing is recorded, and the executor would otherwise
    try to apply all of history.
    """
    recorder = MigrationRecorder(connection)
    recorder.ensure_schema()
    applied = set(recorder.applied_migrations())
    for key in MigrationExecutor(connection).loader.disk_migrations:
        if key not in applied:
            recorder.record_applied(*key)


def migrate_back(targets):
    """Migrate back to ``targets``; return the historical apps there."""
    # Rows are written and tables then altered in one transaction, which
    # PostgreSQL refuses while deferred foreign key checks are pending.
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    _mark_everything_applied()
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


def migrate_forward(app_label="proposal"):
    """Migrate ``app_label`` forward to its latest migration again."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes(app_label))
