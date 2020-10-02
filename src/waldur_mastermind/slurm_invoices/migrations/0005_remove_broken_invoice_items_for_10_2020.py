from django.contrib.contenttypes.models import ContentType
from django.db import migrations

from waldur_mastermind.invoices import models
from waldur_slurm.models import Allocation


def remove_invoice_items(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    InvoiceItem.objects.filter(
        content_type_id=ContentType.objects.get_for_model(Allocation).id,
        invoice__state=models.Invoice.States.PENDING,
        name='',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('slurm_invoices', '0004_remove_broken_invoice_items_for_allocation'),
    ]

    operations = [migrations.RunPython(remove_invoice_items)]
