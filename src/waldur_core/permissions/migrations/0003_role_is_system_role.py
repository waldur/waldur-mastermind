from django.db import migrations, models


def fill_system_roles(apps, schema_editor):
    Role = apps.get_model('permissions', 'Role')
    Role.objects.all().update(is_system_role=True)


class Migration(migrations.Migration):
    dependencies = [
        ('permissions', '0002_import_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='role',
            name='is_system_role',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(fill_system_roles),
    ]
