"""Encrypt the credential-named values inside ServiceSettings.options.

``options`` mixes ordinary backend configuration with real credentials that some
plugins keep there rather than in the dedicated ``password`` / ``token`` columns —
``client_secret`` (Azure service principal), ``keycloak_password``, ``vault_token``.
Only those values are encrypted; endpoints, tenant ids and tuning flags stay
plaintext, so nothing that reads an option changes behaviour.

The column type does not change (``options`` is a text column holding serialised
JSON), so this is metadata-only plus the backfill below.

The credential predicate and the token-shape check are **frozen copies** of the live
``core.encryption`` ones. A historical migration must keep encrypting exactly the keys
it encrypted the day it was written: importing the live classifier would let a later
addition silently change what an already-applied migration would have done, and make a
re-run disagree with the rows it wrote the first time. Only ``encrypt_value`` is
imported live, because it reads the deployment's key configuration, which is not
history.
"""

import json

from django.db import migrations

import waldur_core.core.fields
from waldur_core.core.encryption import encrypt_value

# Frozen copies of core.encryption as of this migration.
_CREDENTIAL_KEYS = frozenset({"password", "token", "secret"})
_CREDENTIAL_SUFFIXES = ("_password", "_token", "_secret")
_TOKEN_PREFIX = "gAAAA"

# Rows are read in keyset batches so a large table is never materialised at once.
BATCH_SIZE = 500


def _is_credential_key(key) -> bool:
    return isinstance(key, str) and (
        key in _CREDENTIAL_KEYS or key.endswith(_CREDENTIAL_SUFFIXES)
    )


def _is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_TOKEN_PREFIX)


def _iter_rows(connection, table):
    """Stream (id, options) in id-ordered batches, closing the cursor between them."""
    last_id = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id, options FROM {table} "  # noqa: S608 (name from _meta)
                "WHERE options IS NOT NULL AND options NOT IN ('', '{}') "
                "AND id > %s ORDER BY id LIMIT %s",
                [last_id, BATCH_SIZE],
            )
            batch = cursor.fetchall()
        if not batch:
            return
        yield from batch
        last_id = batch[-1][0]


def encrypt_existing_options(apps, schema_editor):
    ServiceSettings = apps.get_model("structure", "ServiceSettings")

    # Read the raw at-rest value through a cursor, bypassing the field's decrypting
    # from_db_value, so already-encrypted values are detected and skipped.
    for pk, raw in _iter_rows(schema_editor.connection, ServiceSettings._meta.db_table):
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        encrypted = {
            key: encrypt_value(value)
            if (
                _is_credential_key(key)
                and isinstance(value, str)
                and value
                and not _is_encrypted(value)
            )
            else value
            for key, value in data.items()
        }
        if encrypted == data:
            continue
        # .update() bypasses the field's pre_save, storing the values verbatim.
        ServiceSettings.objects.filter(pk=pk).update(options=encrypted)


class Migration(migrations.Migration):
    dependencies = [
        ("structure", "0080_encrypt_servicesettings_credentials"),
    ]

    operations = [
        migrations.AlterField(
            model_name="servicesettings",
            name="options",
            field=waldur_core.core.fields.EncryptedOptionsField(
                blank=True, default=dict, help_text="Extra options"
            ),
        ),
        migrations.RunPython(
            encrypt_existing_options, migrations.RunPython.noop, elidable=True
        ),
    ]
