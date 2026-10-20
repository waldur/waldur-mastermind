"""Helpers for migrations that touch stored settings.

django-constance's ``constance_constance`` table holds settings as
``{"__type__": ..., "__value__": ...}`` JSON envelopes. ``configuration.0002``
copies them into ``configuration_setting``. Migrations written against that
table run only while it exists; a fresh database never has it.
"""

import json

CONSTANCE_TABLE = "constance_constance"
SETTING_TABLE = "configuration_setting"

MISSING = object()


def table_exists(connection, name):
    return name in connection.introspection.table_names()


def decode_constance(raw):
    """The value a constance row holds, or MISSING for a row without one.

    A value that isn't an envelope, such as a bare file name, is returned
    as is.
    """
    if raw is None:
        return MISSING
    try:
        decoded = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(decoded, dict) and set(decoded) == {"__type__", "__value__"}:
        return decoded["__value__"]
    return decoded


def encode_constance(value):
    return json.dumps({"__type__": "default", "__value__": value})


def write_setting(connection, key, value):
    """Store ``value`` for ``key`` in every settings table that exists.

    Before ``configuration.0002`` the constance table is the one read, and its
    rows are copied over later; afterwards the setting table is. Writing both
    keeps the value whichever side of that migration this runs on. Without
    either table there is nowhere to write yet: on a fresh database
    ``configuration.0002`` derives what it needs itself.
    """
    with connection.cursor() as cursor:
        if table_exists(connection, CONSTANCE_TABLE):
            cursor.execute(f"DELETE FROM {CONSTANCE_TABLE} WHERE key = %s", [key])
            cursor.execute(
                f"INSERT INTO {CONSTANCE_TABLE} (key, value) VALUES (%s, %s)",
                [key, encode_constance(value)],
            )
        if table_exists(connection, SETTING_TABLE):
            cursor.execute(f"DELETE FROM {SETTING_TABLE} WHERE key = %s", [key])
            cursor.execute(
                f"INSERT INTO {SETTING_TABLE} "
                "(key, value, source, source_detail, updated_at) "
                "VALUES (%s, %s, 'migration', '', now())",
                [key, json.dumps(value)],
            )
