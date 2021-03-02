from django.db import migrations

SLURM_TYPE = 'SlurmInvoices.SlurmPackage'


def drop_invalid_slurm_invoice_items(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    Resource = apps.get_model('marketplace', 'Resource')
    slurm_allocations = Resource.objects.filter(offering__type=SLURM_TYPE).values_list(
        'id', flat=True
    )
    InvoiceItem.objects.filter(
        resource_id__in=slurm_allocations,
        invoice__year=2021,
        invoice__month=2,
        measured_unit='',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0047_migrate_slurm_measured_unit'),
    ]

    operations = [
        migrations.RunPython(drop_invalid_slurm_invoice_items),
    ]
