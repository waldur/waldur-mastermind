import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def safe_create_monthly_invoices(apps, schema_editor):
    """
    Safely run invoice processing without email notifications during migration.
    This migration ensures that any field dependencies (like checklist fields)
    are handled gracefully and NO EMAILS are sent during deployment.
    """
    try:
        # Import inside function to avoid import-time issues during migration
        from django.conf import settings
        from django.db import transaction
        from django.db.models import Q
        from django.utils import timezone

        from waldur_core.core import utils as core_utils
        from waldur_core.structure import models as structure_models
        from waldur_mastermind.invoices import models, registrators
        from waldur_mastermind.marketplace.tasks import (
            copy_future_price_to_current_price,
        )

        logger.info(
            "Running migration-safe invoice processing (NO EMAILS will be sent)"
        )

        # Check if we actually need to run this (if previous migration had issues)
        CustomerCredit = apps.get_model("invoices", "CustomerCredit")
        credits_exist = CustomerCredit.objects.filter(end_date__isnull=False).exists()

        if not credits_exist:
            logger.info("No credits found, skipping invoice processing")
            return

        # Replicate create_monthly_invoices logic but WITHOUT email sending
        copy_future_price_to_current_price()
        date = timezone.now()

        # Process old invoices
        old_invoices = models.Invoice.objects.filter(
            Q(state=models.Invoice.States.PENDING, year__lt=date.year)
            | Q(
                state=models.Invoice.States.PENDING,
                year=date.year,
                month__lt=date.month,
            )
        )

        # Import the functions we need
        from waldur_mastermind.invoices.tasks import (
            process_invoice_credits,
            set_to_zero_overdue_credits,
        )

        set_to_zero_overdue_credits()

        processed_count = 0
        for invoice in old_invoices:
            try:
                with transaction.atomic():
                    process_invoice_credits(invoice)
                    invoice.set_created()
                    processed_count += 1
            except Exception as e:
                logger.warning("Unable to process invoice %s: %s", invoice, e)
                continue

        # Create new invoices for current month
        customers = structure_models.Customer.objects.exclude(archived=True)
        if settings.WALDUR_CORE["ENABLE_ACCOUNTING_START_DATE"]:
            customers = customers.filter(accounting_start_date__lt=timezone.now())

        created_count = 0
        for customer in customers.iterator():
            try:
                invoice, created = (
                    registrators.RegistrationManager.get_or_create_invoice(
                        customer, core_utils.month_start(date)
                    )
                )
                if created:
                    created_count += 1
            except Exception as e:
                logger.warning(
                    "Unable to create monthly invoice for customer %s: %s", customer, e
                )

        logger.info(
            "Migration invoice processing completed: processed %d old invoices, created %d new invoices",
            processed_count,
            created_count,
        )
        logger.info(
            "IMPORTANT: Email notifications were SKIPPED during migration - no spam sent!"
        )

    except Exception as e:
        # Log the error but don't fail the migration
        logger.warning("Failed to run migration-safe invoice processing: %s", e)
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
