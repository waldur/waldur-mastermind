import uuid

from django.db import migrations, models

import waldur_core.core.fields


def gen_uuid(apps, schema_editor):
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')
    for row in OfferingComponent.objects.all():
        row.uuid = uuid.uuid4().hex
        row.save(update_fields=['uuid'])


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0094_resource_error_message'),
    ]

    operations = [
        migrations.AddField(
            model_name='offeringcomponent',
            name='uuid',
            field=models.UUIDField(null=True),
        ),
        migrations.RunPython(gen_uuid, elidable=True),
        migrations.AlterField(
            model_name='offeringcomponent',
            name='uuid',
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
