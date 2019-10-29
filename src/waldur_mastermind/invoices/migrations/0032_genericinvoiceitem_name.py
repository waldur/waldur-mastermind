# -*- coding: utf-8 -*-
from django.db import migrations, models


def get_name(item):
    if item.details.get('name'):
        return item.details.get('name')
    if item.details:
        return ', '.join(['%s: %s' % (k, v) for k, v in item.details.items()])
    if item.content_type:
        return '%s.%s' % (item.content_type.app_label, item.content_type.model)
    return ''


def init_invoice_item_name(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    for item in InvoiceItem.objects.all():
        item.name = get_name(item)
        item.save(update_fields=['name'])


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0031_rename_invoice_item_model'),
    ]

    operations = [
        migrations.AddField(
            model_name='InvoiceItem',
            name='name',
            field=models.TextField(default=''),
        ),
        migrations.RunPython(init_invoice_item_name),
    ]
