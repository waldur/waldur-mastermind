from django.db import migrations


def sort_schedules(apps, schema_editor):
    from waldur_mastermind.booking.utils import sort_attributes_schedules
    from waldur_mastermind.booking import PLUGIN_NAME

    Resource = apps.get_model('marketplace', 'Resource')

    for resource in Resource.objects.filter(offering__type=PLUGIN_NAME):
        sort_attributes_schedules(resource.attributes)
        resource.save()


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('marketplace', '0035_offeringpermission'),
    ]

    operations = [
        migrations.RunPython(sort_schedules),
    ]
