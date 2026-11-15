from django.db import migrations

# ProjectKind values when this migration was written.
VALID_KINDS = ("default", "course", "public")


def invalid_kind_to_default(apps, schema_editor):
    """Give projects whose kind is not a ProjectKind choice the model default.

    import_structure stored whatever kind the import file had, an empty string
    when it had none, and clients generated from the API schema failed to parse
    such projects.
    """
    Project = apps.get_model("structure", "Project")
    Project._base_manager.exclude(kind__in=VALID_KINDS).update(kind="default")


class Migration(migrations.Migration):
    dependencies = [
        ("structure", "0089_enable_project_digest_notification"),
    ]

    operations = [
        migrations.RunPython(invalid_kind_to_default, migrations.RunPython.noop),
    ]
