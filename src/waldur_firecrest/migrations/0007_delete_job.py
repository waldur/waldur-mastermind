from django.db import migrations


def delete_job_content_type(apps, schema_editor):
    # Django keeps a deleted model's content type and permissions; drop them,
    # so an upgraded database matches one built from the models.
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="waldur_firecrest", model="job").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("waldur_firecrest", "0006_alter_job_options"),
    ]

    operations = [
        migrations.RunPython(delete_job_content_type, migrations.RunPython.noop),
        migrations.DeleteModel(
            name="Job",
        ),
    ]
