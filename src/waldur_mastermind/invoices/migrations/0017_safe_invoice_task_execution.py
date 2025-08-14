import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def safe_create_monthly_invoices(apps, schema_editor):
    """
    Safely run create_monthly_invoices task with proper error handling.
    This migration ensures that any field dependencies (like checklist fields)
    are handled gracefully during deployment.
    """
    try:
        # Import inside function to avoid import-time issues during migration
        from waldur_mastermind.invoices.tasks import create_monthly_invoices

        # Check if we actually need to run this (if previous migration had issues)
        CustomerCredit = apps.get_model("invoices", "CustomerCredit")

        # Only run if there are credits that might need invoice processing
        credits_exist = CustomerCredit.objects.filter(end_date__isnull=False).exists()

        if credits_exist:
            create_monthly_invoices()
            logger.info("Successfully ran create_monthly_invoices task")
        else:
            logger.info("No credits found, skipping create_monthly_invoices task")

    except Exception as e:
        # Log the error but don't fail the migration
        logger.warning(
            "Failed to run create_monthly_invoices task during migration: %s", e
        )
        logger.info(
            "This is not critical - the task can be run manually later if needed"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0016_alter_invoiceitem_unit"),
        (
            "structure",
            "0056_customer_project_metadata_checklist",
        ),  # Ensure checklist field exists
    ]

    operations = [migrations.RunPython(safe_create_monthly_invoices)]
