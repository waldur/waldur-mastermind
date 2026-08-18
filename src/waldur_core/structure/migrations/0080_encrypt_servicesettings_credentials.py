"""Encrypt ServiceSettings.password / token at rest.

Widens both columns to TextField (the ciphertext is far longer than the plaintext
they used to hold) and encrypts whatever was written before the field became
``EncryptedTextField``.

Reads the raw at-rest value through a cursor, bypassing the field's decrypting
``from_db_value``, so already-encrypted rows are skipped — idempotent, and safe to
run after some rows were already encrypted by live writes.

The token-shape check is a **frozen copy** of ``core.encryption.is_encrypted``: a
historical migration must keep behaving the way it did the day it was written, even
if the live helper changes. Only ``encrypt_value`` is imported live, because it
reads the deployment's key configuration, which is not history.
"""

from django.db import migrations

import waldur_core.core.fields
from waldur_core.core.encryption import encrypt_value

# Frozen copy of core.encryption._FERNET_TOKEN_PREFIX.
_TOKEN_PREFIX = "gAAAA"

# Rows are read in keyset batches so a large table is never materialised at once.
BATCH_SIZE = 500


def _is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_TOKEN_PREFIX)


def _iter_rows(connection, table):
    """Stream (id, password, token) in id-ordered batches.

    Keyset pagination rather than one ``fetchall()``, so the whole credential table is
    never held in memory at once. Each batch closes its cursor before the caller
    writes, so reads and writes never interleave on one open cursor.
    """
    last_id = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id, password, token FROM {table} "  # noqa: S608 (name from _meta)
                "WHERE (password IS NOT NULL OR token IS NOT NULL) "
                "AND id > %s ORDER BY id LIMIT %s",
                [last_id, BATCH_SIZE],
            )
            batch = cursor.fetchall()
        if not batch:
            return
        yield from batch
        last_id = batch[-1][0]


def encrypt_existing_credentials(apps, schema_editor):
    ServiceSettings = apps.get_model("structure", "ServiceSettings")

    for pk, password, token in _iter_rows(
        schema_editor.connection, ServiceSettings._meta.db_table
    ):
        updates = {}
        if password and not _is_encrypted(password):
            updates["password"] = encrypt_value(password)
        if token and not _is_encrypted(token):
            updates["token"] = encrypt_value(token)
        if updates:
            # .update() bypasses the field's pre_save, storing the ciphertext verbatim.
            ServiceSettings.objects.filter(pk=pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [
        ("structure", "0079_accesssubnet_scopes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="servicesettings",
            name="password",
            field=waldur_core.core.fields.EncryptedTextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="servicesettings",
            name="token",
            field=waldur_core.core.fields.EncryptedTextField(blank=True, null=True),
        ),
        migrations.RunPython(
            encrypt_existing_credentials, migrations.RunPython.noop, elidable=True
        ),
    ]
