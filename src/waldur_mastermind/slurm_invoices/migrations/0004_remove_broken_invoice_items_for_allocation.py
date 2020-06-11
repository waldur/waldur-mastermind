from django.contrib.contenttypes.models import ContentType
from django.db import migrations

from waldur_mastermind.invoices import models
from waldur_slurm.models import Allocation


def drop_broken_items(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    InvoiceItem.objects.filter(
        content_type_id=ContentType.objects.get_for_model(Allocation).id,
        invoice__state=models.Invoice.States.PENDING,
        invoice__month='6',
        invoice__year='2020',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('slurm_invoices', '0003_increase_price_precision'),
    ]

    operations = [migrations.RunPython(drop_broken_items)]
