from django.db import migrations


def migrate_slurm_measured_unit(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')

    for item in InvoiceItem.objects.filter(name__contains=' (RAM GB-hours)'):
        item.name = item.name.replace(' (RAM GB-hours)', ' / RAM')
        item.measured_unit = 'GB-hours'
        item.save(update_fields=['name', 'measured_unit'])

    for item in InvoiceItem.objects.filter(name__contains=' (CPU-hours)'):
        item.name = item.name.replace(' (CPU-hours)', ' / CPU')
        item.measured_unit = 'hours'
        item.save(update_fields=['name', 'measured_unit'])

    for item in InvoiceItem.objects.filter(name__contains=' (CPU hours)'):
        item.name = item.name.replace(' (CPU hours)', ' / CPU')
        item.measured_unit = 'hours'
        item.save(update_fields=['name', 'measured_unit'])

    for item in InvoiceItem.objects.filter(name__contains=' (GPU-hours)'):
        item.name = item.name.replace(' (GPU-hours)', ' / GPU')
        item.measured_unit = 'hours'
        item.save(update_fields=['name', 'measured_unit'])

    for item in InvoiceItem.objects.filter(name__contains=' (GPU hours)'):
        item.name = item.name.replace(' (GPU hours)', ' / GPU')
        item.measured_unit = 'hours'
        item.save(update_fields=['name', 'measured_unit'])

    for item in InvoiceItem.objects.filter(
        details__has_key='offering_component_measured_unit'
    ):
        if not item.measured_unit:
            item.measured_unit = item.details['offering_component_measured_unit']
            del item.details['offering_component_measured_unit']
            item.save(update_fields=['details', 'measured_unit'])


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0046_invoiceitem_measured_unit'),
    ]

    operations = [
        migrations.RunPython(migrate_slurm_measured_unit),
    ]
