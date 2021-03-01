import math

from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import migrations
from django.db.models import Q

SLURM_TYPE = 'SlurmInvoices.SlurmPackage'

minutes_in_hour = 60

mb_in_gb = 1024


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


def fix_slurm_invoice_usage(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    ComponentUsage = apps.get_model('marketplace', 'ComponentUsage')
    for component_usage in ComponentUsage.objects.filter(
        billing_period='2021-02-01', resource__offering__type=SLURM_TYPE
    ):
        component_type = component_usage.component.type
        try:
            item = (
                InvoiceItem.objects.filter(
                    invoice__year=2021,
                    invoice__month=2,
                    resource=component_usage.resource,
                )
                .filter(
                    Q(details__offering_component_type=component_type)
                    | Q(details__type=component_type)
                )
                .get()
            )
            if component_type == 'ram':
                item.quantity = int(
                    math.ceil(1.0 * component_usage.usage / mb_in_gb / minutes_in_hour)
                )
            else:
                item.quantity = int(
                    math.ceil(1.0 * component_usage.usage / minutes_in_hour)
                )
            item.save()
        except (ObjectDoesNotExist, MultipleObjectsReturned):
            continue


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0047_migrate_slurm_measured_unit'),
    ]

    operations = [
        migrations.RunPython(drop_invalid_slurm_invoice_items),
    ]
