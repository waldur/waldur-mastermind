"""Copy settings out of django-constance's table.

Constance stored a setting's default the first time it was read, so most rows
hold the default rather than anything an administrator chose. A row equal to
the current default is not moved: the setting reads the same either way, and
leaving it unset lets a later change of the default reach it. A deliberate
choice that happens to equal the default cannot be told apart from such a
read, and is treated the same.

Secrets keep their stored form: ciphertext stays as it is, plaintext an older
release left is encrypted.

The table is kept for one release, so pods of the previous release keep
reading it while a rolling upgrade replaces them, and a rollback still finds
it. It holds no secret in clear: core.0053, which this depends on, encrypts
them first. Writes made there after this migration are not copied.

This depends on the last migrations that wrote to constance's table, so on an
upgrade they run first and their writes are moved here. A fresh database never
had that table; for it, the SERVICE_ACCESS_MODE derivation of
marketplace.0258 is run once more, since that may have run before the setting
table existed.
"""

import logging
from importlib import import_module

from django.db import migrations

from waldur_core.configuration import registry, secrets
from waldur_core.configuration.migration_utils import (
    CONSTANCE_TABLE,
    MISSING,
    decode_constance,
    table_exists,
)
from waldur_core.core import encryption

logger = logging.getLogger(__name__)


def _stored_secret(key, value):
    """The value to store for secret ``key``, or MISSING if it is the default."""
    if not isinstance(value, str) or not value:
        return MISSING if value == registry.default(key) else value
    if not encryption.is_encrypted(value):
        if value == registry.default(key):
            return MISSING
        return secrets.encrypt_secret(value)
    plaintext = secrets.decrypt_stored(value)
    # A value no configured key decrypts is kept as it is: it may become
    # readable again once the key is restored.
    if plaintext is not None and plaintext == registry.default(key):
        return MISSING
    return value


def import_settings(apps, schema_editor):
    connection = schema_editor.connection
    Setting = apps.get_model("configuration", "Setting")

    if not table_exists(connection, CONSTANCE_TABLE):
        derive = import_module(
            "waldur_mastermind.marketplace.migrations.0258_derive_service_access_mode"
        ).derive_mode
        derive(apps, schema_editor)
        return

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT key, value FROM {CONSTANCE_TABLE}")
        rows = cursor.fetchall()

    already_set = set(Setting.objects.values_list("key", flat=True))
    to_create = []
    for key, raw in sorted(rows):
        if not registry.has(key):
            logger.warning("Dropping setting %s, which no longer exists", key)
            continue
        if key in already_set:
            continue
        value = decode_constance(raw)
        if value is MISSING:
            continue
        if registry.is_secret(key):
            value = _stored_secret(key, value)
        elif value == registry.default(key):
            value = MISSING
        if value is MISSING:
            continue
        to_create.append(
            Setting(
                key=key,
                value=value,
                source="migration",
                source_detail="django-constance",
            )
        )
    Setting.objects.bulk_create(to_create)


class Migration(migrations.Migration):
    dependencies = [
        ("configuration", "0001_initial"),
        ("core", "0053_encrypt_constance_secrets"),
        ("marketplace", "0281_rerun_data_migrations_skipped_by_squashes"),
    ]

    operations = [
        migrations.RunPython(import_settings, migrations.RunPython.noop),
    ]
