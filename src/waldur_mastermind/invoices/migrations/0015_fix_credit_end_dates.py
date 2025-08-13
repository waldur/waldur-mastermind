import logging

from dateutil.relativedelta import relativedelta
from django.db import migrations

from waldur_mastermind.invoices.tasks import create_monthly_invoices

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

    # Run the monthly invoice creation task after fixing dates
    if fixed_count > 0:
        try:
            create_monthly_invoices()
            logger.info(
                "Ran create_monthly_invoices task after fixing dates which should fix the invoice states"
            )
        except Exception as e:
            logger.warning("Failed to run create_monthly_invoices task: %s", e)


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0014_remove_VAT_from_compensations"),
        ("structure", "0056_customer_project_metadata_checklist"),
    ]

    operations = [migrations.RunPython(fix_credit_end_dates)]
