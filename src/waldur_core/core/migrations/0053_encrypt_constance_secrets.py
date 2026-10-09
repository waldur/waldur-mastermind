from constance import settings as constance_settings
from constance.codecs import dumps, loads
from django.db import migrations

from waldur_core.core import constance_backend, encryption


def _secret_rows(apps):
    Constance = apps.get_model("constance", "Constance")
    prefix = constance_settings.DATABASE_PREFIX
    keys = [f"{prefix}{key}" for key in constance_backend.secret_keys()]
    return Constance.objects.filter(key__in=keys)


def encrypt_secrets(apps, schema_editor):
    # Rows already encrypted are left alone, so the migration can run again
    # against a database a newer release has partly written.
    for row in _secret_rows(apps):
        value = loads(row.value)
        if isinstance(value, str) and value and not encryption.is_encrypted(value):
            row.value = dumps(encryption.encrypt_value(value))
            row.save(update_fields=["value"])


def decrypt_secrets(apps, schema_editor):
    for row in _secret_rows(apps):
        value = loads(row.value)
        plaintext = constance_backend.decrypt_stored(value)
        # A value no configured key decrypts stays as it is.
        if plaintext is not None and plaintext != value:
            row.value = dumps(plaintext)
            row.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0052_rename_matrix_login_method"),
        ("constance", "0003_drop_pickle"),
    ]

    operations = [migrations.RunPython(encrypt_secrets, decrypt_secrets)]
