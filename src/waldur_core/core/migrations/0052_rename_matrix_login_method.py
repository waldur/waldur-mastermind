import json

from django.db import migrations

OLD_KEY = "MATRIX_LOGIN_METHOD"
NEW_KEY = "MATRIX_EXTERNAL_LOGIN_METHOD"
METHODS = {"none", "password", "oidc"}
# Only the `oidc` method's oidc_provider_url used it, which the credentials
# response no longer returns.
REMOVED_KEYS = ["MATRIX_OIDC_PROVIDER_URL"]


def _stored_value(raw):
    try:
        return json.loads(raw)["__value__"]
    except (TypeError, ValueError, KeyError):
        return None


def _encode(value):
    return json.dumps({"__type__": "default", "__value__": value})


def rename_login_method(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM constance_constance WHERE key = ANY(%s)", [REMOVED_KEYS]
        )
        cursor.execute(
            "SELECT value FROM constance_constance WHERE key = %s", [OLD_KEY]
        )
        row = cursor.fetchone()
        if row is None:
            return
        method = _stored_value(row[0])
        # `token` handed browsers a token no external client can sign in
        # with, so it has no successor; neither has anything unreadable.
        if not (isinstance(method, str) and method in METHODS):
            method = "none"
        # Constance may have created the new key on first access already.
        cursor.execute("DELETE FROM constance_constance WHERE key = %s", [NEW_KEY])
        cursor.execute(
            "UPDATE constance_constance SET key = %s, value = %s WHERE key = %s",
            [NEW_KEY, _encode(method), OLD_KEY],
        )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0051_telemetry_opt_out"),
        ("constance", "0003_drop_pickle"),
    ]

    operations = [
        migrations.RunPython(rename_login_method, migrations.RunPython.noop),
    ]
