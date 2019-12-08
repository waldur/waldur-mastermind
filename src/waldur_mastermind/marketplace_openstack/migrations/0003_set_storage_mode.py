from django.db import migrations


PACKAGE_TYPE = 'Packages.Template'

STORAGE_MODE_FIXED = 'fixed'


def set_storage_mode(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    Offering.objects.filter(type=PACKAGE_TYPE).update(plugin_options={'storage_mode': STORAGE_MODE_FIXED})


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace_openstack', '0002_prefix_storage'),
    ]

    operations = [
        migrations.RunPython(set_storage_mode)
    ]
