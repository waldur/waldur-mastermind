import uuid

from django.db import migrations, models

import waldur_core.core.fields


def gen_uuid(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    for row in InvoiceItem.objects.all():
        row.uuid = uuid.uuid4().hex
        row.save(update_fields=['uuid'])


class Migration(migrations.Migration):
    dependencies = [
        ('invoices', '0052_delete_servicedowntime'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoiceitem',
            name='uuid',
            field=models.UUIDField(null=True),
        ),
        migrations.RunPython(gen_uuid, elidable=True),
        migrations.AlterField(
            model_name='invoiceitem',
            name='uuid',
            field=waldur_core.core.fields.UUIDField(),
        ),
    ]
