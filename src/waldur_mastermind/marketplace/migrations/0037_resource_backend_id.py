from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations, models


def fill_report(apps, schema_editor):
    Resource = apps.get_model('marketplace', 'Resource')
    ContentType = apps.get_model('contenttypes', 'ContentType')

    for resource in Resource.objects.exclude(content_type_id=None):
        ct = ContentType.objects.get_for_id(resource.content_type_id)
        model_class = apps.get_model(ct.app_label, ct.model)
        try:
            scope = model_class.objects.get(id=resource.object_id)
        except ObjectDoesNotExist:
            print(
                f'Unable to get resource scope with object ID: {resource.object_id}, content type ID: {resource.content_type_id}'
            )
            continue
        else:
            if scope.backend_id:
                resource.backend_id = scope.backend_id
                resource.save(update_fields=['backend_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0036_offeringcomponent_backend_id'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='resource',
            name='backend_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.RunPython(fill_report, migrations.RunPython.noop),
    ]
