"""matrix-nio's crypto store on Waldur's own Postgres database.

nio keeps the bot's Olm account, Olm and Megolm sessions, device keys and trust
marks in a peewee database it expects to be SQLite. Run against Postgres, three
things differ, and :class:`PostgresStore` adapts each:

- nio saves rows with SQLite's ``REPLACE``; Postgres needs ``ON CONFLICT … DO
  UPDATE`` on the table's own unique key, which is read off each model;
- ``_get_device_ids`` is raw SQLite SQL (``==``, ``?`` placeholders), replaced by
  the same query in peewee;
- the tables live in their own schema rather than next to Waldur's, qualified
  on every model instead of through ``search_path``, which a transaction-pooling
  PgBouncer would not keep between statements.

nio's models are module globals, so opening a store points them at its schema
for the whole process. Only the bot process opens one.

The store's secrets are pickled under ``pickle_key``. A wrong key makes every
load fail, which must stop the bot: resetting the store would throw away its
identity and every session it holds.

The schema is created here and not by a Django migration: CI builds the test
database from model state alone, and nio creates and upgrades its own tables.
"""

import re

from django.db import connections
from nio.store import SqliteStore
from nio.store.models import DeviceKeys
from peewee import AutoField, OnConflict, Tuple
from playhouse.psycopg3_ext import Psycopg3Database

SCHEMA = "matrix_bot"

# Connection options Django understands but psycopg.connect() does not.
_DJANGO_ONLY_OPTIONS = {"isolation_level", "server_side_binding", "assume_role", "pool"}

_UNIQUE = re.compile(r"UNIQUE\s*\(([^)]*)\)", re.IGNORECASE)

# nio hands at most this many (user, device) pairs to one query.
_DEVICE_ID_BATCH = 150


class StoreVersionMismatch(RuntimeError):
    """The store was written by a nio with another store version."""


def _conflict_target(model):
    """The columns that identify a row of ``model``: its unique key, else its primary key."""
    for constraint in model._meta.constraints or ():
        match = _UNIQUE.search(getattr(constraint, "sql", "") or "")
        if match:
            return [column.strip() for column in match.group(1).split(",")]
    return [model._meta.primary_key.column_name]


class _Database(Psycopg3Database):
    def conflict_update(self, on_conflict, query):
        # nio writes with SQLite's REPLACE: delete the clashing row, insert the
        # new one. The Postgres equivalent updates the row in place.
        if (on_conflict._action or "").lower() == "replace":
            model = query.model
            target = _conflict_target(model)
            preserve = [
                field
                for field in model._meta.sorted_fields
                if not isinstance(field, AutoField) and field.column_name not in target
            ]
            on_conflict = OnConflict(
                conflict_target=target,
                preserve=preserve or None,
                action=None if preserve else "ignore",
            )
        return super().conflict_update(on_conflict, query)


def database_settings(alias="default"):
    """Connection arguments for the Django database ``alias``, as peewee takes them."""
    settings = connections[alias].settings_dict
    kwargs = {
        key: value
        for key, value in (settings.get("OPTIONS") or {}).items()
        if key not in _DJANGO_ONLY_OPTIONS
    }
    for setting, argument in (
        ("HOST", "host"),
        ("PORT", "port"),
        ("USER", "user"),
        ("PASSWORD", "password"),
    ):
        if settings.get(setting):
            kwargs[argument] = settings[setting]
    return settings["NAME"], kwargs


class PostgresStore(SqliteStore):
    """nio's ``SqliteStore`` on Postgres, in the ``schema_name`` schema.

    Takes the same arguments as ``SqliteStore``; ``store_path`` is unused.
    """

    schema_name = SCHEMA
    database_alias = "default"

    def _create_database(self):
        name, kwargs = database_settings(self.database_alias)
        database = _Database(name, **kwargs)
        database.connect()
        try:
            database.execute_sql(f'CREATE SCHEMA IF NOT EXISTS "{self.schema_name}"')
        finally:
            database.close()
        for model in self.models:
            model._meta.schema = self.schema_name
        return database

    def _get_store_version(self):
        # A new store is created at the current version. Any other version was
        # written by another nio: a newer one this nio can't read, or one so old
        # that nio's upgrade, which knows nothing of the schema, would run
        # against whatever search_path finds.
        version = super()._get_store_version()
        if version != self.store_version:
            self.database.close()
            raise StoreVersionMismatch(
                f"The Matrix bot's crypto store is at version {version}, but this "
                f"matrix-nio uses version {self.store_version}. Refusing to open it "
                "rather than risk corrupting it."
            )
        return version

    def _get_device_ids(self, account, devices):
        pairs = [(device.user_id, device.id) for device in devices]
        device_ids = []
        for start in range(0, len(pairs), _DEVICE_ID_BATCH):
            query = DeviceKeys.select(DeviceKeys.id).where(
                (DeviceKeys.account == account.id)
                & Tuple(DeviceKeys.user_id, DeviceKeys.device_id).in_(
                    pairs[start : start + _DEVICE_ID_BATCH]
                )
            )
            device_ids += [row.id for row in query]
        return device_ids
