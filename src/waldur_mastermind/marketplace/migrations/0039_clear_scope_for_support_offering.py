from django.db import migrations


def clear_scope_for_support_offering(apps, schema_editor):
    Resource = apps.get_model('marketplace', 'Resource')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    SupportOffering = apps.get_model('support', 'Offering')

    for resource in Resource.objects.filter(
        content_type_id=ContentType.objects.get_for_model(SupportOffering).id
    ):
        resource.content_type_id = None
        resource.object_id = None
        resource.save(update_fields=['content_type_id', 'object_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0038_resource_report'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('support', '0010_error_traceback'),
    ]

    operations = [
        migrations.RunPython(
            clear_scope_for_support_offering, migrations.RunPython.noop
        ),
    ]
