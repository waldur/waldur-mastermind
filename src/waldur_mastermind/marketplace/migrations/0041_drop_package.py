from django.db import migrations

TENANT_TYPE = 'Packages.Template'


def drop_package(apps, schema_editor):
    Plan = apps.get_model('marketplace', 'Plan')
    Plan.objects.filter(offering__type=TENANT_TYPE).update(
        object_id=None, content_type_id=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0040_resource_description'),
    ]

    operations = [
        migrations.RunPython(drop_package, migrations.RunPython.noop),
    ]
