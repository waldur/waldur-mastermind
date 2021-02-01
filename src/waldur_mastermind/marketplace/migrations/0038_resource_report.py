import django.contrib.postgres.fields.jsonb
from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def fill_report(apps, schema_editor):
    Resource = apps.get_model('marketplace', 'Resource')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    SupportOffering = apps.get_model('support', 'Offering')

    for resource in Resource.objects.filter(
        content_type_id=ContentType.objects.get_for_model(SupportOffering).id
    ):
        try:
            support_offering = SupportOffering.objects.get(id=resource.object_id)
        except ObjectDoesNotExist:
            print(f'Unable to get support offering with ID: {resource.object_id}')
            continue
        if support_offering.report:
            resource.report = support_offering.report
            resource.save(update_fields=['report'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0037_resource_backend_id'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('support', '0010_error_traceback'),
    ]

    operations = [
        migrations.AddField(
            model_name='resource',
            name='report',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(fill_report, migrations.RunPython.noop),
    ]
