from __future__ import unicode_literals

from django.db import migrations, models


def migrate_invoice_item_price(apps, schema_editor):
    OpenStackItem = apps.get_model('invoices', 'OpenStackItem')
    OfferingItem = apps.get_model('invoices', 'OfferingItem')

    OpenStackItem.objects.all().update(unit_price=models.F('daily_price'))
    OfferingItem.objects.all().update(unit_price=models.F('daily_price'))


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0015_add_unit_price'),
        ('support', '0011_remove_price'),
    ]

    operations = [
        migrations.RunPython(migrate_invoice_item_price),
        migrations.RemoveField(
            model_name='offeringitem',
            name='daily_price',
        ),
        migrations.RemoveField(
            model_name='openstackitem',
            name='daily_price',
        ),
    ]
