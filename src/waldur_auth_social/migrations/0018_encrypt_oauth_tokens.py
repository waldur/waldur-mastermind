"""Encrypt stored identity provider credentials at rest.

``OAuthToken.refresh_token`` and ``IdentityProvider.client_secret`` become
``EncryptedTextField``; access tokens are no longer stored at all (they live in
the cache until they expire), so that column is dropped.

Existing values are encrypted by reading the raw at-rest value through a
cursor, bypassing the field's decrypting ``from_db_value``, so already
encrypted rows are skipped and the step is idempotent. As in
``structure/0080_encrypt_servicesettings_credentials``, the token-shape check
is a frozen copy of ``core.encryption.is_encrypted`` and only
``encrypt_value`` (which reads the deployment's keys) is imported live.
"""

from django.db import migrations

import waldur_core.core.fields
from waldur_core.core.encryption import encrypt_value

# Frozen copy of core.encryption._FERNET_TOKEN_PREFIX.
_TOKEN_PREFIX = "gAAAA"


def _is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(_TOKEN_PREFIX)


def _encrypt_column(apps, schema_editor, model_name, column):
    model = apps.get_model("waldur_auth_social", model_name)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id, {column} FROM {model._meta.db_table}"  # noqa: S608 (names from _meta)
        )
        rows = cursor.fetchall()
    for pk, value in rows:
        if value and not _is_encrypted(value):
            # .update() bypasses the field's pre_save, storing the ciphertext verbatim.
            model.objects.filter(pk=pk).update(**{column: encrypt_value(value)})


def encrypt_existing_credentials(apps, schema_editor):
    _encrypt_column(apps, schema_editor, "OAuthToken", "refresh_token")
    _encrypt_column(apps, schema_editor, "IdentityProvider", "client_secret")


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_auth_social", "0017_identityprovider_issuer_jwks_url"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="oauthtoken",
            name="access_token",
        ),
        migrations.AlterField(
            model_name="identityprovider",
            name="client_secret",
            field=waldur_core.core.fields.EncryptedTextField(
                help_text="Application secret key."
            ),
        ),
        migrations.AlterField(
            model_name="oauthtoken",
            name="refresh_token",
            field=waldur_core.core.fields.EncryptedTextField(),
        ),
        migrations.RunPython(
            encrypt_existing_credentials, migrations.RunPython.noop, elidable=True
        ),
    ]
