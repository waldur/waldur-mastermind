from django.db import migrations


def rename_offering_type(apps, schema_editor):
    Offering = apps.get_model('marketplace', 'Offering')
    Offering.objects.filter(type='Packages.Template').update(type='OpenStack.Admin')


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace_openstack', '0012_drop_offering_components'),
    ]

    operations = [migrations.RunPython(rename_offering_type)]
