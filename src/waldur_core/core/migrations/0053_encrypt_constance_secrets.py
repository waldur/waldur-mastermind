from django.db import migrations

from waldur_core.configuration import registry, secrets
from waldur_core.configuration.migration_utils import (
    CONSTANCE_TABLE,
    MISSING,
    decode_constance,
    encode_constance,
    table_exists,
)
from waldur_core.core import encryption


def _secret_rows(cursor):
    cursor.execute(
        f"SELECT key, value FROM {CONSTANCE_TABLE} WHERE key = ANY(%s)",
        [sorted(registry.secret_keys())],
    )
    for key, raw in cursor.fetchall():
        value = decode_constance(raw)
        if value is not MISSING:
            yield key, value


def _update(cursor, key, value):
    cursor.execute(
        f"UPDATE {CONSTANCE_TABLE} SET value = %s WHERE key = %s",
        [encode_constance(value), key],
    )


def encrypt_secrets(apps, schema_editor):
    connection = schema_editor.connection
    if not table_exists(connection, CONSTANCE_TABLE):
        return
    with connection.cursor() as cursor:
        # Rows already encrypted are left alone, so the migration can run again
        # against a database a newer release has partly written.
        for key, value in list(_secret_rows(cursor)):
            if isinstance(value, str) and value and not encryption.is_encrypted(value):
                _update(cursor, key, encryption.encrypt_value(value))


def decrypt_secrets(apps, schema_editor):
    connection = schema_editor.connection
    if not table_exists(connection, CONSTANCE_TABLE):
        return
    with connection.cursor() as cursor:
        for key, value in list(_secret_rows(cursor)):
            plaintext = secrets.decrypt_stored(value)
            # A value no configured key decrypts stays as it is.
            if plaintext is not None and plaintext != value:
                _update(cursor, key, plaintext)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0052_rename_matrix_login_method"),
    ]

    operations = [migrations.RunPython(encrypt_secrets, decrypt_secrets)]
