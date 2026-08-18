"""Backfill: encrypt sensitive values in existing Offering.secret_options rows.

New writes already encrypt via SecretOptionsField (migration 0264); this encrypts
the rows written before that. Idempotent: rows whose sensitive values are already
Fernet tokens are skipped, so it is safe to re-run and safe to run after some rows
were already encrypted by live writes between deploy and this migration.

The sensitivity predicate and the token-shape check are **frozen copies** of the
live ones (``marketplace.secret_options.is_sensitive_key``,
``core.encryption.is_encrypted``). A historical migration must keep encrypting
exactly the keys it encrypted the day it was written: if the live classifier later
gains a key, importing it here would silently change what an already-applied
migration would have done, and a re-run on a partially migrated database would
disagree with the rows it wrote the first time. Only ``encrypt_value`` is imported
live — that reads the deployment's key configuration, which is not history.
"""

import json

from django.db import migrations

from waldur_core.core.encryption import encrypt_value

# Frozen copy of marketplace.secret_options as of this migration.
_SENSITIVE_KEYS = {"password", "token", "argocd_k8s_kubeconfig"}
_SENSITIVE_SUFFIXES = ("_password", "_token")

# Frozen copy of core.encryption._FERNET_TOKEN_PREFIX.
_TOKEN_PREFIX = "gAAAA"

# Rows are read in keyset batches so a large table is never materialised at once.
BATCH_SIZE = 500


def _is_sensitive_key(key) -> bool:
    return isinstance(key, str) and (
        key in _SENSITIVE_KEYS or key.endswith(_SENSITIVE_SUFFIXES)
    )


def _is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_TOKEN_PREFIX)


def _iter_rows(connection, table):
    """Stream (id, secret_options) in id-ordered batches.

    Keyset pagination rather than one ``fetchall()``: an instance with many offerings
    would otherwise hold every secret_options blob in memory at once. Each batch closes
    its cursor before the caller writes, so reads and writes never interleave on one
    open cursor.
    """
    last_id = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id, secret_options FROM {table} "  # noqa: S608 (name from _meta)
                "WHERE secret_options IS NOT NULL AND secret_options <> '{}'::jsonb "
                "AND id > %s ORDER BY id LIMIT %s",
                [last_id, BATCH_SIZE],
            )
            batch = cursor.fetchall()
        if not batch:
            return
        yield from batch
        last_id = batch[-1][0]


def encrypt_existing_secret_options(apps, schema_editor):
    Offering = apps.get_model("marketplace", "Offering")
    table = Offering._meta.db_table

    # The raw at-rest value is read through a cursor, bypassing the field's decrypting
    # from_db_value, so already-encrypted rows can be detected and skipped.
    for pk, raw in _iter_rows(schema_editor.connection, table):
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            continue
        # A token-shaped value here is genuinely already encrypted (this is the raw
        # column, not user input) and must not be wrapped a second time.
        encrypted = {
            key: encrypt_value(value)
            if (
                _is_sensitive_key(key)
                and isinstance(value, str)
                and value
                and not _is_encrypted(value)
            )
            else value
            for key, value in data.items()
        }
        if encrypted == data:
            continue
        # .update() bypasses the field's pre_save, so the dict is stored verbatim.
        Offering.objects.filter(pk=pk).update(secret_options=encrypted)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0264_alter_offering_secret_options"),
    ]

    operations = [
        migrations.RunPython(
            encrypt_existing_secret_options,
            migrations.RunPython.noop,
            elidable=True,
        ),
    ]
