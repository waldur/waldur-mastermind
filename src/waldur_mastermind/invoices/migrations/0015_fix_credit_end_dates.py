import logging

from dateutil.relativedelta import relativedelta
from django.db import migrations

logger = logging.getLogger(__name__)


def fix_credit_end_dates(apps, schema_editor):
    CustomerCredit = apps.get_model("invoices", "CustomerCredit")

    # Date can be null so just in case we filter out nulls
    credits_to_fix = CustomerCredit.objects.filter(end_date__isnull=False).exclude(
        end_date__day=1
    )

    fixed_count = 0
    for credit in credits_to_fix:
        old_end_date = credit.end_date
        new_end_date = (old_end_date + relativedelta(months=1)).replace(day=1)
        credit.end_date = new_end_date
        credit.save(update_fields=["end_date"])
        fixed_count += 1

        logger.info(
            "Fixed credit end_date: For customer %s, old_end_date %s, new_end_date %s",
            credit.customer.name,
            old_end_date,
            credit.end_date,
        )

    logger.info("Fixed %d credit end dates", fixed_count)

    # Note: create_monthly_invoices task execution moved to migration 0017_safe_invoice_task_execution
    # to ensure proper dependency handling with checklist fields
    if fixed_count > 0:
        logger.info(
            "Fixed %d credit end dates. Invoice task execution moved to next migration.",
            fixed_count,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0014_remove_VAT_from_compensations"),
    ]

    operations = [migrations.RunPython(fix_credit_end_dates)]
