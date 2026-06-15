import logging

from dateutil.relativedelta import relativedelta
from django.db import migrations

logger = logging.getLogger(__name__)


def fix_project_credit_end_dates(apps, schema_editor):
    """Roll mid-month project credit end dates forward to the first day
    of the following month.

    Migration 0015 normalized customer credit end dates after the
    day-must-be-1 validation was introduced, but project credits were
    missed. Surviving mid-month rows fail model validation on save and
    break credit processing.
    """
    ProjectCredit = apps.get_model("invoices", "ProjectCredit")

    credits_to_fix = ProjectCredit.objects.filter(end_date__isnull=False).exclude(
        end_date__day=1
    )

    fixed_count = 0
    for credit in credits_to_fix:
        old_end_date = credit.end_date
        credit.end_date = (old_end_date + relativedelta(months=1)).replace(day=1)
        credit.save(update_fields=["end_date"])
        fixed_count += 1

        logger.info(
            "Fixed project credit end_date: project %s, old_end_date %s, new_end_date %s",
            credit.project.name,
            old_end_date,
            credit.end_date,
        )

    logger.info("Fixed %d project credit end dates", fixed_count)


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0023_add_pending_finalization_state"),
    ]

    operations = [migrations.RunPython(fix_project_credit_end_dates)]
