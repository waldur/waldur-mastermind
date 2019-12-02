from django.db import migrations


def prefix_storage(apps, schema_editor):
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')
    VolumeType = apps.get_model('openstack', 'VolumeType')
    ContentType = apps.get_model('contenttypes', 'ContentType')

    content_type = ContentType.objects.get_for_model(VolumeType)
    for offering_component in OfferingComponent.objects.filter(content_type=content_type):
        if offering_component.name.startswith('Storage ('):
            continue
        offering_component.name = 'Storage (%s)' % offering_component.name
        offering_component.save(update_fields=['name'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace_openstack', '0001_initial'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('openstack', '0002_volumetype'),
    ]

    operations = [
        migrations.RunPython(prefix_storage)
    ]
