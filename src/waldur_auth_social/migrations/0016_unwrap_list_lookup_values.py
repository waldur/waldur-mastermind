import ast

from django.db import migrations


def parse_stringified_list(value):
    """Return the strings of a stringified list of strings, else None."""
    if not (value.startswith("[") and value.endswith("]")):
        return None
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return None
    if isinstance(parsed, list) and parsed and all(isinstance(v, str) for v in parsed):
        return parsed
    return None


def unwrap_list_lookup_values(apps, schema_editor):
    """
    List-valued lookup claims used to be stored as their Python repr, e.g. a
    username of ``['user@example.com']``. Lookups now unwrap the claim, so the
    stored value must be unwrapped too, or the next login misses the account
    and creates a second one.
    """
    IdentityProvider = apps.get_model("waldur_auth_social", "IdentityProvider")
    User = apps.get_model("core", "User")
    # Deactivated users must be fixed too: they can be reactivated.
    users = User._base_manager

    string_fields = {
        field.name
        for field in User._meta.concrete_fields
        if field.get_internal_type() in ("CharField", "TextField")
    }
    lookup_fields = (
        set(IdentityProvider.objects.values_list("user_field", flat=True))
        & string_fields
    )

    for field in sorted(lookup_fields):
        candidates = users.filter(
            **{f"{field}__startswith": "[", f"{field}__endswith": "]"}
        ).values_list("pk", field)
        for pk, value in candidates:
            parsed = parse_stringified_list(value)
            if parsed is None:
                continue
            if len(parsed) > 1:
                print(
                    f"[MIGRATION] User (id={pk}): {field} {value!r} left unchanged, "
                    "it holds several values."
                )
                continue
            (unwrapped,) = parsed
            if not unwrapped:
                continue
            if users.filter(**{field: unwrapped}).exclude(pk=pk).exists():
                print(
                    f"[MIGRATION] User (id={pk}): {field} {value!r} left unchanged, "
                    f"{unwrapped!r} already belongs to another user."
                )
                continue
            users.filter(pk=pk).update(**{field: unwrapped})
            print(
                f"[MIGRATION] User (id={pk}): {field} changed from {value!r} "
                f"to {unwrapped!r}."
            )


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_auth_social", "0015_alter_identityprovider_options"),
        ("core", "0050_notificationtemplate_path_unique"),
    ]

    operations = [
        migrations.RunPython(unwrap_list_lookup_values, migrations.RunPython.noop),
    ]
