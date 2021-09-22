from django.db import migrations

from waldur_mastermind.common.utils import parse_datetime

TERMINATED = 6


def get_full_days(start, end):
    seconds_in_day = 24 * 60 * 60
    full_days, extra_seconds = divmod((end - start).total_seconds(), seconds_in_day)
    if extra_seconds > 0:
        full_days += 1

    return int(full_days)


def fix_resource_limit_periods(apps, schema_editor):
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')

    for item in InvoiceItem.objects.filter(resource__state=TERMINATED):
        if not item.details:
            continue
        resource_limit_periods = item.details.get('resource_limit_periods')
        if not resource_limit_periods:
            continue
        changed = False
        for period in resource_limit_periods:
            if parse_datetime(period['end']) > item.end:
                period['end'] = item.end.isoformat()
                period['billing_periods'] = get_full_days(
                    parse_datetime(period['start']), item.end
                )
        if changed:
            item.save(update_fields=['details'])


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0053_invoiceitem_uuid'),
    ]

    operations = [
        migrations.RunPython(fix_resource_limit_periods),
    ]
