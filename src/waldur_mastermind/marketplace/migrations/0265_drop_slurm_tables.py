from django.db import migrations

# Dropping the waldur_slurm app from INSTALLED_APPS leaves its three tables behind:
# Django only ever removes tables it is told to remove, and an app it no longer
# knows about cannot be told.
#
# Leaving them is not merely untidy. waldur_slurm.Allocation subclassed
# structure_models.BaseResource, so waldur_slurm_allocation still carries real
# FK constraints to structure_project and structure_servicesettings. Django
# performs cascades in Python, not via ON DELETE CASCADE, so once the app is
# gone the ORM no longer knows these rows exist -- and deleting a Project or
# ServiceSettings that still has an allocation row fails at the database with an
# IntegrityError the ORM cannot explain.
#
# 0264 nullified the *generic* scopes pointing at these rows; this migration
# removes the rows and the constraints themselves.

DROP_TABLES = """
DROP TABLE IF EXISTS waldur_slurm_allocationuserusage CASCADE;
DROP TABLE IF EXISTS waldur_slurm_association CASCADE;
DROP TABLE IF EXISTS waldur_slurm_allocation CASCADE;
"""


def drop_slurm_content_types(apps, schema_editor):
    """Remove the now-dangling content types for the deleted app.

    0264 needed these rows to find the scopes it nullified, so they could not be
    removed until after it ran. This cascades to the matching auth.Permission
    rows, which is what `remove_stale_contenttypes` would do anyway.
    """
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="waldur_slurm").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0264_nullify_slurm_resource_scopes"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        # Irreversible: reversing restores the migration record but not the data.
        migrations.RunSQL(DROP_TABLES, migrations.RunSQL.noop),
        migrations.RunPython(drop_slurm_content_types, migrations.RunPython.noop),
    ]
