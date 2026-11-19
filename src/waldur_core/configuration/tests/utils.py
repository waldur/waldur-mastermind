import logging
from contextlib import contextmanager

from django.db import connection

from waldur_core.configuration.migration_utils import CONSTANCE_TABLE, table_exists


class ConstanceTableMixin:
    """Gives a test django-constance's settings table.

    Migrations written against it run on databases that have it. The table is
    created inside the test's transaction and goes away with it.
    """

    def setUp(self):
        super().setUp()
        if not table_exists(connection, CONSTANCE_TABLE):
            with connection.cursor() as cursor:
                cursor.execute(
                    f"CREATE TABLE {CONSTANCE_TABLE} ("
                    "id serial PRIMARY KEY, "
                    "key varchar(255) NOT NULL UNIQUE, "
                    "value text NULL)"
                )


@contextmanager
def logged_setting_changes():
    """Collect the system log records of setting changes made in the block."""
    records = []

    class Collector(logging.Handler):
        def emit(self, record):
            if hasattr(record, "setting_key"):
                records.append(record)

    target = logging.getLogger("waldur_core.core.handlers")
    handler = Collector(level=logging.INFO)
    previous_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)
