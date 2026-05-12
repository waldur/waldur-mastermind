"""
Celery tasks for Arrow billing synchronization and reconciliation.

This module provides tasks for:
- Billing export sync (existing)
- Real-time consumption sync (new)
- Billing export check and reconciliation (new)
"""

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from celery import shared_task
from constance import config
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import models
from .backend import ArrowBackendError, ArrowClient, ArrowCredentials, get_arrow_client

logger = logging.getLogger(__name__)


# -------------------- Scheduled Tasks --------------------


@shared_task(name="waldur_mastermind.waldur_arrow.sync_arrow_billing_scheduled")
def sync_arrow_billing_scheduled():
    """
    Scheduled task to sync Arrow billing for the current month.

    Runs every ARROW_SYNC_INTERVAL_HOURS hours.
    """
    settings = models.ArrowSettings.get_active()
    if not settings:
        logger.info("No active Arrow settings found, skipping sync")
        return

    if not settings.sync_enabled:
        logger.info("Arrow sync is disabled, skipping")
        return

    today = date.today()
    sync_arrow_billing.delay(
        year=today.year,
        month=today.month,
    )


@shared_task(name="waldur_mastermind.waldur_arrow.check_validated_billing")
def check_validated_billing():
    """
    Scheduled task to check for newly validated billing in Arrow.

    Checks synced but not yet validated billing syncs and updates their state.
    If auto-reconciliation is enabled, triggers reconciliation.
    """
    settings = models.ArrowSettings.get_active()
    if not settings:
        logger.info("No active Arrow settings found, skipping validation check")
        return

    if not settings.sync_enabled:
        logger.info("Arrow sync is disabled, skipping validation check")
        return

    client = get_arrow_client()
    if not client:
        return

    # Find synced but not validated billing
    syncs = models.ArrowBillingSync.objects.filter(
        customer_mapping__settings=settings,
        state=models.ArrowBillingSync.States.SYNCED,
    )

    for sync in syncs:
        try:
            # Re-fetch billing data to check state
            # Arrow marks billing as validated typically after month end
            period = sync.report_period
            client.export_billing_sync(
                export_type_reference=settings.export_type_reference,
                period_from=period,
                period_to=period,
                page=1,
            )

            # Check if Arrow has validated the billing
            # This is typically indicated in the billing data metadata
            # For now, we assume billing is validated after the month ends
            period_year, period_month = map(int, period.split("-"))
            period_end = date(period_year, period_month, 1) + core_utils.month_delta(1)

            if date.today() >= period_end:
                sync.arrow_state = "validated"
                sync.mark_validated()
                sync.save()
                logger.info(f"Marked billing sync {sync.uuid} as validated")

                # Trigger auto-reconciliation if enabled
                if getattr(config, "ARROW_AUTO_RECONCILIATION", False):
                    reconcile_arrow_billing.delay(
                        year=period_year,
                        month=period_month,
                    )

        except ArrowBackendError as e:
            logger.warning(f"Failed to check validation for {sync.uuid}: {e}")


# -------------------- Consumption Sync Tasks --------------------


@shared_task(name="waldur_mastermind.waldur_arrow.sync_arrow_consumption_scheduled")
def sync_arrow_consumption_scheduled():
    """
    Scheduled task to sync real-time consumption data from Arrow.

    Runs every ARROW_CONSUMPTION_SYNC_INTERVAL_HOURS (default: hourly).
    Updates ArrowConsumptionRecord and ComponentUsage for each resource with
    an arrow_license_reference attribute.
    """
    if not getattr(config, "ARROW_CONSUMPTION_SYNC_ENABLED", False):
        logger.info("Arrow consumption sync is disabled, skipping")
        return

    settings = models.ArrowSettings.get_active()
    if not settings:
        logger.info("No active Arrow settings found, skipping consumption sync")
        return

    if not settings.sync_enabled:
        logger.info("Arrow sync is disabled in settings, skipping consumption sync")
        return

    today = date.today()
    sync_arrow_consumption.delay(
        year=today.year,
        month=today.month,
    )


@shared_task(name="waldur_mastermind.waldur_arrow.sync_arrow_consumption")
def sync_arrow_consumption(year: int, month: int, settings_uuid: str = ""):
    """
    Sync real-time consumption data from Arrow for a specific period.

    For each resource with arrow_license_reference:
    1. Call get_monthly_consumption(license_ref, current_period)
    2. Aggregate total sell/buy amounts
    3. Update/create ArrowConsumptionRecord
    4. Update ComponentUsage (triggers invoice item via signal)
    5. Skip if record already finalized (billing arrived)

    Args:
        year: Year of the billing period
        month: Month of the billing period
        settings_uuid: Optional specific settings UUID to use
    """
    if settings_uuid:
        settings = models.ArrowSettings.objects.filter(uuid=settings_uuid).first()
    else:
        settings = models.ArrowSettings.get_active()

    if not settings:
        logger.error("No Arrow settings found")
        return {"error": "No Arrow settings found"}

    period = f"{year:04d}-{month:02d}"
    billing_period = date(year, month, 1)
    logger.info(f"Starting Arrow consumption sync for period {period}")

    credentials = ArrowCredentials(
        api_url=settings.api_url,
        api_key=settings.api_key,
    )
    client = ArrowClient(credentials)

    # Find all resources with arrow_license_reference attribute
    resources = marketplace_models.Resource.objects.filter(
        attributes__arrow_license_reference__isnull=False,
        state=marketplace_models.Resource.States.OK,
    ).exclude(attributes__arrow_license_reference="")

    results = {
        "synced": 0,
        "skipped_finalized": 0,
        "errors": [],
    }

    for resource in resources:
        license_ref = resource.attributes.get("arrow_license_reference")
        if not license_ref:
            continue

        # Check if record already finalized
        existing = models.ArrowConsumptionRecord.objects.filter(
            resource=resource,
            billing_period=billing_period,
            license_reference=license_ref,
        ).first()

        if existing and existing.is_finalized:
            logger.debug(
                f"Skipping finalized record for {resource.name} ({license_ref})"
            )
            results["skipped_finalized"] += 1
            continue

        try:
            _sync_resource_consumption(
                client=client,
                resource=resource,
                license_ref=license_ref,
                billing_period=billing_period,
                period=period,
                price_source=settings.invoice_price_source,
                prefix=settings.invoice_item_prefix or "Arrow consumption",
            )
            results["synced"] += 1
        except Exception as e:
            logger.warning(
                f"Failed to sync consumption for {resource.name} ({license_ref}): {e}"
            )
            results["errors"].append(
                {
                    "resource_uuid": str(resource.uuid),
                    "license_reference": license_ref,
                    "error": str(e),
                }
            )

    logger.info(
        f"Consumption sync complete: {results['synced']} synced, "
        f"{results['skipped_finalized']} skipped (finalized), "
        f"{len(results['errors'])} errors"
    )
    return results


def _sync_resource_consumption(
    client: ArrowClient,
    resource: marketplace_models.Resource,
    license_ref: str,
    billing_period: date,
    period: str,
    price_source: str = models.PriceSources.SELL,
    prefix: str = "Arrow consumption",
):
    """
    Sync consumption data for a single resource.

    Fetches consumption data from Arrow API, updates ArrowConsumptionRecord,
    and creates/updates ComponentUsage for billing.
    """
    # Fetch consumption data from Arrow
    consumed_sell = Decimal("0")
    consumed_buy = Decimal("0")
    try:
        consumption_data = client.get_monthly_consumption(
            license_reference=license_ref,
            period_from=period,
            period_to=period,
        )
        consumption_lines = client.parse_consumption_to_dicts(consumption_data)
    except ArrowBackendError as e:
        logger.warning(f"Consumption API unavailable for {license_ref}: {e}")
        # Fall back to prediction API
        try:
            prediction_data = client.get_consumption_prediction(
                license_reference=license_ref,
                granularity="monthly",
            )
            consumed_sell, consumed_buy = _extract_prediction_totals(prediction_data)
            consumption_lines = []  # No detailed lines from prediction
        except ArrowBackendError:
            logger.warning(f"Prediction API also unavailable for {license_ref}")
            raise

    # Aggregate sell/buy totals from consumption lines
    if consumption_lines:
        total_sell = sum(
            _parse_decimal(line.get("Total sell price", 0))
            for line in consumption_lines
        )
        total_buy = sum(
            _parse_decimal(line.get("Total buy price", 0)) for line in consumption_lines
        )
    else:
        total_sell = consumed_sell
        total_buy = consumed_buy

    # Skip if no consumption data at all (license didn't exist in this period)
    if total_sell == 0 and total_buy == 0:
        logger.debug(f"No consumption data for {license_ref} in {period}, skipping")
        return False

    with transaction.atomic():
        # Update or create ArrowConsumptionRecord
        record, created = (
            models.ArrowConsumptionRecord.objects.select_for_update().get_or_create(
                resource=resource,
                billing_period=billing_period,
                license_reference=license_ref,
                defaults={
                    "consumed_sell": total_sell,
                    "consumed_buy": total_buy,
                    "last_sync_at": timezone.now(),
                    "raw_data": {"consumption_lines_count": len(consumption_lines)},
                },
            )
        )

        if not created:
            record.consumed_sell = total_sell
            record.consumed_buy = total_buy
            record.last_sync_at = timezone.now()
            record.raw_data = {"consumption_lines_count": len(consumption_lines)}
            record.save(
                update_fields=[
                    "consumed_sell",
                    "consumed_buy",
                    "last_sync_at",
                    "raw_data",
                    "modified",
                ]
            )

        # Update ComponentUsage for billing
        _update_component_usage_from_consumption(resource, billing_period, total_sell)

        # Create/update invoice item
        _update_provisional_invoice_item(
            record, resource, price_source=price_source, prefix=prefix
        )

    logger.debug(
        f"Synced consumption for {resource.name}: sell={total_sell}, buy={total_buy}"
    )
    return True


def _extract_prediction_totals(prediction_data: dict) -> tuple[Decimal, Decimal]:
    """Extract consumed totals from prediction API response."""
    values = prediction_data.get("values", [])
    total_sell = Decimal("0")
    total_buy = Decimal("0")

    for value in values:
        consumed = value.get("consumed") or {}
        if consumed.get("sell"):
            total_sell += _parse_decimal(consumed["sell"])
        if consumed.get("buy"):
            total_buy += _parse_decimal(consumed["buy"])

    return total_sell, total_buy


def _update_component_usage_from_consumption(
    resource: marketplace_models.Resource,
    billing_period: date,
    sell_amount: Decimal,
):
    """
    Update ComponentUsage from consumption data.

    This triggers the billing invoice item creation/update via signals.
    """
    from waldur_mastermind.marketplace.utils import get_or_create_plan_period

    # Get cloud_cost component from offering
    component = resource.offering.components.filter(type="cloud_cost").first()
    if not component:
        logger.warning(
            f"No cloud_cost component found for offering {resource.offering.uuid}"
        )
        return

    plan_period = get_or_create_plan_period(resource, billing_period)

    usage_date = timezone.make_aware(
        timezone.datetime(billing_period.year, billing_period.month, 1, 0, 0, 0)
    )

    marketplace_models.ComponentUsage.objects.update_or_create(
        resource=resource,
        component=component,
        billing_period=billing_period,
        defaults={
            "usage": sell_amount,
            "date": usage_date,
            "plan_period": plan_period,
            "description": f"Arrow consumption for {billing_period.strftime('%Y-%m')}",
        },
    )


def _update_provisional_invoice_item(
    record: models.ArrowConsumptionRecord,
    resource: marketplace_models.Resource,
    price_source: str = models.PriceSources.SELL,
    prefix: str = "Arrow consumption",
):
    """
    Create or update provisional invoice item from consumption record.

    The invoice item has source="arrow_consumption" in details.

    Args:
        record: The consumption record
        resource: The marketplace resource
        price_source: Which price to use for unit_price ("sell" or "buy")
        prefix: Prefix for invoice item name (e.g. "Arrow consumption")
    """
    customer = resource.project.customer
    year = record.billing_period.year
    month = record.billing_period.month

    unit_price = (
        record.consumed_buy
        if price_source == models.PriceSources.BUY
        else record.consumed_sell
    )

    # Get or create invoice
    invoice, _ = invoices_models.Invoice.objects.get_or_create(
        customer=customer,
        year=year,
        month=month,
    )

    # Check for existing invoice item
    if record.invoice_item:
        # Update existing item
        record.invoice_item.unit_price = unit_price
        record.invoice_item.details = record.get_invoice_item_details()
        record.invoice_item.save(update_fields=["unit_price", "details"])
    else:
        # Create new invoice item
        item = invoices_models.InvoiceItem.objects.create(
            invoice=invoice,
            resource=resource,
            project=resource.project,
            project_name=resource.project.name,
            project_uuid=resource.project.uuid.hex,
            name=f"{prefix}: {resource.name}"[:255],
            unit_price=unit_price,
            quantity=Decimal("1"),
            unit=invoices_models.InvoiceItem.Units.QUANTITY,
            details=record.get_invoice_item_details(),
        )
        record.invoice_item = item
        record.save(update_fields=["invoice_item"])

    invoice.update_cache()


@shared_task(name="waldur_mastermind.waldur_arrow.check_billing_export_scheduled")
def check_billing_export_scheduled():
    """
    Scheduled task to check for finalized billing export and reconcile.

    Runs every ARROW_BILLING_CHECK_INTERVAL_HOURS (default: 6 hours).
    Checks previous month and current month for billing data.
    """
    if not getattr(config, "ARROW_CONSUMPTION_SYNC_ENABLED", False):
        logger.info("Arrow consumption sync is disabled, skipping billing check")
        return

    settings = models.ArrowSettings.get_active()
    if not settings:
        logger.info("No active Arrow settings found, skipping billing check")
        return

    if not settings.sync_enabled:
        logger.info("Arrow sync is disabled in settings, skipping billing check")
        return

    today = date.today()

    # Check previous month (most likely to have finalized data)
    if today.month == 1:
        prev_year = today.year - 1
        prev_month = 12
    else:
        prev_year = today.year
        prev_month = today.month - 1

    check_and_reconcile_billing.delay(year=prev_year, month=prev_month)

    # Also check current month (may have partial data)
    check_and_reconcile_billing.delay(year=today.year, month=today.month)


@shared_task(name="waldur_mastermind.waldur_arrow.check_and_reconcile_billing")
def check_and_reconcile_billing(
    year: int,
    month: int,
    settings_uuid: str = "",
    force_reconcile: bool = False,
):
    """
    Check for finalized billing and reconcile consumption records.

    Args:
        year: Year of the billing period
        month: Month of the billing period
        settings_uuid: Optional specific settings UUID to use
        force_reconcile: If True, reconcile even if already reconciled
    """
    if settings_uuid:
        settings = models.ArrowSettings.objects.filter(uuid=settings_uuid).first()
    else:
        settings = models.ArrowSettings.get_active()

    if not settings:
        logger.error("No Arrow settings found")
        return {"error": "No Arrow settings found"}

    period = f"{year:04d}-{month:02d}"
    billing_period = date(year, month, 1)
    logger.info(f"Checking billing export for period {period}")

    credentials = ArrowCredentials(
        api_url=settings.api_url,
        api_key=settings.api_key,
    )
    client = ArrowClient(credentials)

    # Fetch billing export data
    try:
        billing_data = client.export_billing_all_pages(
            export_type_reference=settings.export_type_reference,
            period_from=period,
            period_to=period,
        )
        billing_lines = client.parse_billing_export_to_dicts(billing_data)
    except ArrowBackendError as e:
        logger.warning(f"Failed to fetch billing export for {period}: {e}")
        return {"error": str(e)}

    if not billing_lines:
        logger.info(f"No billing data available for {period}")
        return {"status": "no_data"}

    logger.info(f"Found {len(billing_lines)} billing lines")

    # Find consumption records to reconcile
    records_filter = Q(
        billing_period=billing_period,
        finalized_at__isnull=True,
    )
    if not force_reconcile:
        records_filter &= Q(reconciled_at__isnull=True)

    records = models.ArrowConsumptionRecord.objects.filter(records_filter)

    results = {
        "finalized": 0,
        "reconciled": 0,
        "compensation_items_created": 0,
        "no_billing_data": 0,
        "errors": [],
    }

    for record in records:
        try:
            # Find billing data for this record by license reference
            billing_info = _find_billing_by_license_ref(
                billing_lines, record.license_reference
            )

            if not billing_info:
                logger.debug(
                    f"No billing data for {record.license_reference}, skipping"
                )
                results["no_billing_data"] += 1
                continue

            compensation_created = _reconcile_consumption_record(
                record=record,
                billing_info=billing_info,
                force=force_reconcile,
                price_source=settings.invoice_price_source,
                prefix=settings.invoice_item_prefix or "Arrow consumption",
            )

            results["finalized"] += 1
            if compensation_created:
                results["reconciled"] += 1
                results["compensation_items_created"] += 1

        except Exception as e:
            logger.warning(f"Failed to reconcile {record.uuid}: {e}")
            results["errors"].append(
                {
                    "record_uuid": str(record.uuid),
                    "error": str(e),
                }
            )

    logger.info(
        f"Billing check complete for {period}: "
        f"{results['finalized']} finalized, {results['reconciled']} reconciled"
    )
    return results


def _find_billing_by_license_ref(
    billing_lines: list[dict],
    license_reference: str,
) -> dict | None:
    """
    Find billing data by license reference.

    Different Arrow export types store the XSP reference in different fields:
    'License Reference' or 'ARS Subscription ID'. We check both.

    Returns aggregated billing info or None.
    """
    matching_lines = [
        line
        for line in billing_lines
        if line.get("ARS Subscription ID") == license_reference
    ]

    if not matching_lines:
        return None

    sell_total = sum(
        _parse_decimal(line.get("Customer Total Price", 0)) for line in matching_lines
    )
    buy_total = sum(
        _parse_decimal(line.get("Total Wholesale Price", 0)) for line in matching_lines
    )

    return {
        "sell_total": sell_total,
        "buy_total": buy_total,
        "lines": matching_lines,
    }


def _reconcile_consumption_record(
    record: models.ArrowConsumptionRecord,
    billing_info: dict,
    force: bool = False,
    price_source: str = models.PriceSources.SELL,
    prefix: str = "Arrow consumption",
) -> bool:
    """
    Reconcile a consumption record with finalized billing data.

    Creates compensation invoice item in CURRENT month if there's a difference.

    Args:
        record: The consumption record to reconcile
        billing_info: Dict with sell_total, buy_total from billing export
        force: If True, reconcile even if already reconciled
        price_source: Which price pair to use for adjustment ("sell" or "buy")
        prefix: Prefix for invoice item name

    Returns:
        True if compensation item was created, False otherwise
    """
    final_sell = billing_info["sell_total"]
    final_buy = billing_info["buy_total"]

    with transaction.atomic():
        # Lock the record
        record = models.ArrowConsumptionRecord.objects.select_for_update().get(
            pk=record.pk
        )

        # Skip if already reconciled (unless forced)
        if record.is_reconciled and not force:
            logger.debug(f"Record {record.uuid} already reconciled, skipping")
            return False

        # Update final amounts
        record.final_sell = final_sell
        record.final_buy = final_buy
        record.finalized_at = timezone.now()

        # Calculate adjustment based on price source
        if price_source == models.PriceSources.BUY:
            adjustment = final_buy - record.consumed_buy
        else:
            adjustment = final_sell - record.consumed_sell

        compensation_created = False

        # Create compensation if significant difference (>= 0.01)
        if abs(adjustment) >= Decimal("0.01"):
            compensation_created = _create_compensation_item(
                record, adjustment, prefix=prefix
            )

        record.reconciled_at = timezone.now()
        record.save()

        # Update the original invoice item details
        if record.invoice_item:
            record.invoice_item.details = record.get_finalized_details()
            record.invoice_item.save(update_fields=["details"])

    logger.info(
        f"Reconciled {record.uuid}: consumed={record.consumed_sell}, "
        f"final={final_sell}, adjustment={adjustment}"
    )
    return compensation_created


def _create_compensation_item(
    record: models.ArrowConsumptionRecord,
    adjustment: Decimal,
    prefix: str = "Arrow consumption",
) -> bool:
    """
    Create compensation invoice item in CURRENT month.

    Args:
        record: The consumption record
        adjustment: The adjustment amount (positive = additional charge, negative = credit)
        prefix: Prefix for invoice item name

    Returns:
        True if item was created
    """
    resource = record.resource
    customer = resource.project.customer

    # Get current month invoice
    today = date.today()
    current_invoice, _ = invoices_models.Invoice.objects.get_or_create(
        customer=customer,
        year=today.year,
        month=today.month,
    )

    # Determine description
    if adjustment > 0:
        desc = f"{prefix} adjustment: {resource.name} (additional charge for {record.billing_period})"
    else:
        desc = (
            f"{prefix} adjustment: {resource.name} (credit for {record.billing_period})"
        )

    # Create compensation item
    compensation_item = invoices_models.InvoiceItem.objects.create(
        invoice=current_invoice,
        resource=resource,
        project=resource.project,
        project_name=resource.project.name,
        project_uuid=resource.project.uuid.hex,
        name=desc[:255],
        unit_price=adjustment,
        quantity=Decimal("1"),
        unit=invoices_models.InvoiceItem.Units.QUANTITY,
        details=record.get_compensation_details(),
    )

    record.compensation_item = compensation_item
    record.save(update_fields=["compensation_item"])

    current_invoice.update_cache()

    logger.info(
        f"Created compensation item {compensation_item.uuid} for {record.uuid}: {adjustment}"
    )
    return True


# -------------------- Manual Trigger Tasks --------------------


@shared_task(name="waldur_mastermind.waldur_arrow.sync_arrow_billing")
def sync_arrow_billing(
    year: int, month: int, settings_uuid: str = "", resource_uuid: str = ""
):
    """
    Sync Arrow billing for a specific period.

    Args:
        year: Year of the billing period
        month: Month of the billing period
        settings_uuid: Optional specific settings UUID to use
        resource_uuid: Optional resource UUID to filter billing lines for
    """
    if settings_uuid:
        settings = models.ArrowSettings.objects.filter(uuid=settings_uuid).first()
    else:
        settings = models.ArrowSettings.get_active()

    if not settings:
        # Arrow integration not configured for this deployment — emit a warning
        # rather than an error so it does not pollute production error logs.
        logger.warning("No Arrow settings found")
        return

    if not settings.export_type_reference:
        logger.warning(
            "Arrow settings has no export_type_reference configured. "
            "Please set it in the Arrow Integration settings."
        )
        return

    period = f"{year:04d}-{month:02d}"
    logger.info(f"Starting Arrow billing sync for period {period}")

    credentials = ArrowCredentials(
        api_url=settings.api_url,
        api_key=settings.api_key,
    )
    client = ArrowClient(credentials)

    # Get all active customer mappings
    mappings = models.ArrowCustomerMapping.objects.filter(
        settings=settings,
        is_active=True,
    ).select_related("waldur_customer")

    if not mappings.exists():
        logger.info("No active customer mappings found")
        return

    try:
        # Fetch all billing data for the period
        billing_data = client.export_billing_all_pages(
            export_type_reference=settings.export_type_reference,
            period_from=period,
            period_to=period,
        )

        billing_lines = client.parse_billing_export_to_dicts(billing_data)
        logger.info(f"Retrieved {len(billing_lines)} billing lines from Arrow")

        # If resource_uuid is specified, resolve its license reference for filtering
        target_license_ref = ""
        if resource_uuid:
            resource = marketplace_models.Resource.objects.filter(
                uuid=resource_uuid
            ).first()
            if resource and resource.backend_id:
                target_license_ref = resource.backend_id
                logger.info(
                    f"Filtering billing lines for resource {resource_uuid} "
                    f"(license ref: {target_license_ref})"
                )
            else:
                logger.warning(
                    f"Resource {resource_uuid} not found or has no backend_id, "
                    "skipping resource filter"
                )

        # Group billing lines by customer company name
        lines_by_name: dict[str, list[dict]] = {}
        for line in billing_lines:
            company_name = line.get("End User Company Name", "")
            if company_name:
                lines_by_name.setdefault(company_name, []).append(line)

        # Process each customer mapping
        for mapping in mappings:
            customer_lines = lines_by_name.get(mapping.arrow_company_name, [])
            if target_license_ref:
                customer_lines = [
                    line
                    for line in customer_lines
                    if line.get("ARS Subscription ID") == target_license_ref
                ]
            if customer_lines:
                _process_customer_billing(
                    settings=settings,
                    mapping=mapping,
                    lines=customer_lines,
                    period=period,
                    year=year,
                    month=month,
                    price_source=settings.invoice_price_source,
                )

    except ArrowBackendError as e:
        logger.error(f"Arrow billing sync failed: {e}")


def _process_customer_billing(
    settings: models.ArrowSettings,
    mapping: models.ArrowCustomerMapping,
    lines: list[dict],
    period: str,
    year: int,
    month: int,
    price_source: str = models.PriceSources.SELL,
):
    """
    Process billing lines for a single customer.

    Creates/updates ArrowBillingSync and ArrowBillingSyncItem records,
    and creates corresponding InvoiceItems.
    """
    customer = mapping.waldur_customer

    # Build vendor -> offering mapping cache
    vendor_offering_map = {}
    for vom in models.ArrowVendorOfferingMapping.objects.filter(
        settings=settings, is_active=True
    ).select_related("offering"):
        vendor_offering_map[vom.arrow_vendor_name] = vom.offering

    # Get or create invoice for the customer/period
    invoice, _ = invoices_models.Invoice.objects.get_or_create(
        customer=customer,
        year=year,
        month=month,
    )

    # Get or create billing sync record
    # Use the first line's statement reference, or generate one
    statement_ref = lines[0].get(
        "Statement Reference", f"SYNC-{period}-{mapping.arrow_reference}"
    )

    billing_sync, created = models.ArrowBillingSync.objects.get_or_create(
        customer_mapping=mapping,
        statement_reference=statement_ref,
        defaults={
            "report_period": period,
            "invoice": invoice,
            "arrow_state": "pending",
        },
    )

    if not created and billing_sync.state >= models.ArrowBillingSync.States.VALIDATED:
        # Don't update validated/reconciled syncs
        logger.info(f"Skipping already validated sync {billing_sync.uuid}")
        return

    total_buy = Decimal("0")
    total_sell = Decimal("0")

    with transaction.atomic():
        for line in lines:
            try:
                line_ref = line.get("Sequence") or line.get("Order Id", "")
                if not line_ref:
                    continue

                # Check for existing sync item
                sync_item = models.ArrowBillingSyncItem.objects.filter(
                    billing_sync=billing_sync,
                    arrow_line_reference=line_ref,
                ).first()

                # Parse amounts — field names vary by export type
                sell_price = _parse_decimal(
                    line.get("Sell Total Price")
                    or line.get("Customer Total Price")
                    or "0"
                )
                buy_price = _parse_decimal(
                    line.get("Buy Total Price")
                    or line.get("Total Wholesale Price")
                    or "0"
                )
                quantity = _parse_decimal(
                    line.get("Quantity") or line.get("Qty") or "1"
                )
                unit_price = (
                    buy_price if price_source == models.PriceSources.BUY else sell_price
                )

                if unit_price == Decimal("0"):
                    # Skip zero-amount items
                    continue

                total_buy += buy_price
                total_sell += sell_price

                # Build item description
                vendor = line.get("Vendor Name", "") or line.get("Service Name", "")
                product = (
                    line.get("Product Name")
                    or line.get("Friendly Name")
                    or line.get("Description", "")
                )
                description = f"{vendor} - {product}" if vendor else product

                # Look up linked resource by ARS subscription ID
                license_ref_value = line.get("ARS Subscription ID", "")
                resource = None
                project = None
                if license_ref_value:
                    resource = marketplace_models.Resource.objects.filter(
                        attributes__arrow_license_reference=license_ref_value,
                        project__customer=customer,
                    ).first()
                if resource:
                    project = resource.project
                else:
                    # Fall back to offering from vendor mapping to find a default project
                    offering = vendor_offering_map.get(vendor)
                    if offering:
                        # Try to find any project for this customer
                        project = structure_models.Project.objects.filter(
                            customer=customer,
                        ).first()

                if sync_item:
                    # Update existing item
                    if sync_item.original_price != unit_price:
                        # Price changed - update
                        sync_item.invoice_item.unit_price = unit_price
                        sync_item.invoice_item.save(update_fields=["unit_price"])
                        # Note: don't update original_price - that's for reconciliation
                else:
                    # Create invoice item
                    invoice_item = invoices_models.InvoiceItem.objects.create(
                        invoice=invoice,
                        resource=resource,
                        project=project,
                        project_name=project.name if project else "",
                        project_uuid=project.uuid.hex if project else "",
                        name=description[:255],
                        unit_price=unit_price,
                        quantity=quantity,
                        unit=invoices_models.InvoiceItem.Units.QUANTITY,
                        details={
                            "source": "arrow",
                            "arrow_line_reference": line_ref,
                            "vendor_name": vendor,
                            "subscription_reference": line.get(
                                "Vendor Subscription ID", ""
                            ),
                            "classification": line.get("Classification", ""),
                        },
                    )

                    # Create sync item
                    models.ArrowBillingSyncItem.objects.create(
                        billing_sync=billing_sync,
                        arrow_line_reference=line_ref,
                        invoice_item=invoice_item,
                        original_price=unit_price,
                        vendor_name=vendor[:255],
                        subscription_reference=line.get("Vendor Subscription ID", "")[
                            :255
                        ],
                        classification=line.get("Classification", "")[:50],
                        description=description[:500],
                        quantity=quantity,
                    )

            except Exception as e:
                logger.warning(f"Failed to process billing line: {e}")

        # Update billing sync totals
        billing_sync.buy_total = total_buy
        billing_sync.sell_total = total_sell
        billing_sync.report_period = period

        if created or billing_sync.state == models.ArrowBillingSync.States.PENDING:
            billing_sync.mark_synced()

        billing_sync.save()

        # Update invoice cache
        invoice.update_cache()

    logger.info(
        f"Processed {len(lines)} billing lines for {mapping.arrow_reference}, "
        f"total sell: {total_sell}"
    )


@shared_task(name="waldur_mastermind.waldur_arrow.reconcile_arrow_billing")
def reconcile_arrow_billing(
    year: int,
    month: int,
    settings_uuid: str = "",
    force: bool = False,
):
    """
    Reconcile Arrow billing for a specific period.

    Creates compensation invoice items in the current month for price differences.

    Args:
        year: Year of the billing period to reconcile
        month: Month of the billing period to reconcile
        settings_uuid: Optional specific settings UUID to use
        force: If True, reconcile even if not validated
    """
    if settings_uuid:
        settings = models.ArrowSettings.objects.filter(uuid=settings_uuid).first()
    else:
        settings = models.ArrowSettings.get_active()

    if not settings:
        logger.error("No Arrow settings found")
        return

    period = f"{year:04d}-{month:02d}"
    logger.info(f"Starting Arrow billing reconciliation for period {period}")

    # Find syncs that need reconciliation
    state_filter = (
        [
            models.ArrowBillingSync.States.VALIDATED,
            models.ArrowBillingSync.States.SYNCED,
        ]
        if force
        else [models.ArrowBillingSync.States.VALIDATED]
    )

    syncs = models.ArrowBillingSync.objects.filter(
        customer_mapping__settings=settings,
        report_period=period,
        state__in=state_filter,
    ).select_related("customer_mapping__waldur_customer")

    if not syncs.exists():
        logger.info(f"No syncs found for reconciliation in period {period}")
        return

    # Get current month for compensation items
    today = date.today()
    current_year = today.year
    current_month = today.month

    # Fetch current billing data from Arrow
    client = get_arrow_client()
    if not client:
        logger.error("Failed to get Arrow client")
        return

    try:
        billing_data = client.export_billing_all_pages(
            export_type_reference=settings.export_type_reference,
            period_from=period,
            period_to=period,
        )
        billing_lines = client.parse_billing_export_to_dicts(billing_data)

        # Create lookup by line reference
        # Field names vary by export type
        sell_field = (
            "Sell Total Price"
            if "Sell Total Price" in (billing_lines[0] if billing_lines else {})
            else "Customer Total Price"
        )
        buy_field = (
            "Buy Total Price"
            if "Buy Total Price" in (billing_lines[0] if billing_lines else {})
            else "Total Wholesale Price"
        )
        price_field = (
            buy_field
            if settings.invoice_price_source == models.PriceSources.BUY
            else sell_field
        )
        current_prices: dict[str, Decimal] = {}
        for line in billing_lines:
            line_ref = line.get("Sequence") or line.get("Order Id", "")
            if line_ref:
                current_prices[line_ref] = _parse_decimal(line.get(price_field, "0"))

    except ArrowBackendError as e:
        logger.error(f"Failed to fetch current billing data: {e}")
        return

    # Process each sync
    prefix = settings.invoice_item_prefix or "Arrow consumption"
    for sync in syncs:
        _reconcile_sync(
            sync=sync,
            current_prices=current_prices,
            current_year=current_year,
            current_month=current_month,
            prefix=prefix,
        )


def _reconcile_sync(
    sync: models.ArrowBillingSync,
    current_prices: dict[str, Decimal],
    current_year: int,
    current_month: int,
    prefix: str = "Arrow consumption",
):
    """
    Reconcile a single billing sync.

    Creates compensation invoice items for price differences.
    """
    customer = sync.customer_mapping.waldur_customer

    # Get or create current month invoice for compensations
    current_invoice, _ = invoices_models.Invoice.objects.get_or_create(
        customer=customer,
        year=current_year,
        month=current_month,
    )

    compensations_created = 0

    with transaction.atomic():
        for item in sync.items.filter(compensation_item__isnull=True):
            current_price = current_prices.get(item.arrow_line_reference)
            if current_price is None:
                # Line no longer exists - might need credit
                current_price = Decimal("0")

            price_diff = current_price - item.original_price

            if price_diff == Decimal("0"):
                # No difference, skip
                continue

            # Create compensation invoice item
            if price_diff > 0:
                description = (
                    f"{prefix} adjustment: {item.description} (additional charge)"
                )
            else:
                description = f"{prefix} adjustment: {item.description} (credit)"

            # Carry over resource/project from original invoice item
            original_resource = (
                item.invoice_item.resource if item.invoice_item else None
            )
            original_project = item.invoice_item.project if item.invoice_item else None

            compensation_item = invoices_models.InvoiceItem.objects.create(
                invoice=current_invoice,
                resource=original_resource,
                project=original_project,
                project_name=original_project.name if original_project else "",
                project_uuid=original_project.uuid.hex if original_project else "",
                name=description[:255],
                unit_price=price_diff,
                quantity=Decimal("1"),
                unit=invoices_models.InvoiceItem.Units.QUANTITY,
                details={
                    "source": "arrow_reconciliation",
                    "original_line_reference": item.arrow_line_reference,
                    "original_price": str(item.original_price),
                    "final_price": str(current_price),
                    "original_period": sync.report_period,
                },
            )

            item.compensation_item = compensation_item
            item.save(update_fields=["compensation_item"])
            compensations_created += 1

        if compensations_created > 0:
            sync.mark_reconciled()
            sync.save()
            current_invoice.update_cache()

    logger.info(
        f"Reconciled sync {sync.uuid}: created {compensations_created} compensation items"
    )


def _group_billing_by_subscription(
    billing_lines: list[dict],
) -> dict[str, dict]:
    """
    Group billing lines by Vendor Subscription ID.

    Returns a dict keyed by subscription ID with aggregated sell/buy totals
    and the list of original lines.
    """
    result: dict[str, dict] = {}
    for line in billing_lines:
        sub_id = line.get("Vendor Subscription ID", "")
        if not sub_id:
            continue
        if sub_id not in result:
            result[sub_id] = {
                "sell_total": Decimal("0"),
                "buy_total": Decimal("0"),
                "lines": [],
            }
        sell = _parse_decimal(
            line.get("Sell Total Price") or line.get("Customer Total Price") or "0"
        )
        buy = _parse_decimal(
            line.get("Buy Total Price") or line.get("Total Wholesale Price") or "0"
        )
        result[sub_id]["sell_total"] += sell
        result[sub_id]["buy_total"] += buy
        result[sub_id]["lines"].append(line)
    return result


def _parse_decimal(value: str | int | float | Decimal) -> Decimal:
    """Parse a value to Decimal, handling various formats."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if not value:
        return Decimal("0")

    # Handle string values
    value_str = str(value).strip()

    # Remove currency symbols and whitespace
    for char in ["€", "$", "£", " ", ","]:
        value_str = value_str.replace(char, "")

    try:
        return Decimal(value_str)
    except InvalidOperation:
        logger.warning(f"Failed to parse decimal value: {value}")
        return Decimal("0")


# -------------------- Resource Sync Tasks --------------------


@shared_task(name="waldur_mastermind.waldur_arrow.sync_arrow_resources")
def sync_arrow_resources(
    period_from: str,
    period_to: str,
    settings_uuid: str = "",
    offering_uuid: str = "",
    project_uuid: str = "",
    force_import: bool = False,
):
    """
    Sync Arrow IAAS subscriptions to Waldur Resources.

    Matches Arrow subscriptions by Vendor Subscription ID to Waldur Resource backend_id.
    Updates resource report and current_usages fields with aggregated billing data.

    Args:
        period_from: Start period in YYYY-MM format
        period_to: End period in YYYY-MM format
        settings_uuid: Optional specific Arrow settings UUID
        offering_uuid: Optional offering UUID for new resources
        project_uuid: Optional project UUID for new resources (ignored if force_import=True)
        force_import: If True, auto-create Customers, Projects, and Offering from Arrow data
    """
    from waldur_core.structure import models as structure_models
    from waldur_mastermind.marketplace import models as marketplace_models

    if settings_uuid:
        settings = models.ArrowSettings.objects.filter(uuid=settings_uuid).first()
    else:
        settings = models.ArrowSettings.get_active()

    if not settings:
        logger.error("No Arrow settings found")
        return {"error": "No Arrow settings found"}

    logger.info(
        f"Syncing Arrow resources for period {period_from} to {period_to} "
        f"(force_import={force_import})"
    )

    # Get Arrow client
    credentials = ArrowCredentials(
        api_url=settings.api_url,
        api_key=settings.api_key,
    )
    client = ArrowClient(credentials)

    # Fetch billing data
    try:
        export_data = client.export_billing_all_pages(
            export_type_reference=settings.export_type_reference,
            period_from=period_from,
            period_to=period_to,
        )
    except ArrowBackendError as e:
        logger.error(f"Failed to fetch Arrow billing: {e}")
        return {"error": str(e)}

    # Aggregate by subscription (with customer details for force_import)
    subscriptions, customers = _aggregate_subscriptions_for_resources(
        export_data, include_customer_details=force_import
    )
    logger.info(
        f"Found {len(subscriptions)} IAAS subscriptions from {len(customers)} customers"
    )

    # Results tracking
    results = {
        "synced": 0,
        "created": 0,
        "updated": 0,
        "orders_created": 0,
        "customers_created": 0,
        "projects_created": 0,
        "mappings_created": 0,
        "invoices_created": 0,
        "invoice_items_created": 0,
        "errors": [],
    }

    if force_import:
        # Force import mode: create customers, projects, offerings, mappings, and invoices
        offering = _get_or_create_arrow_offering(offering_uuid)
        if not offering:
            return {"error": "Failed to get or create offering"}

        plan = offering.plans.first()

        # Process each customer
        for customer_name, customer_info in customers.items():
            try:
                waldur_customer, customer_created = _get_or_create_customer_from_arrow(
                    customer_info
                )
                if customer_created:
                    results["customers_created"] += 1

                project, project_created = _get_or_create_project_for_customer(
                    waldur_customer
                )
                if project_created:
                    results["projects_created"] += 1

                # Create ArrowCustomerMapping
                mapping, mapping_created = _get_or_create_customer_mapping(
                    settings=settings,
                    arrow_reference=customer_info.get("arrow_id", customer_name[:50]),
                    arrow_company_name=customer_name,
                    waldur_customer=waldur_customer,
                )
                if mapping_created:
                    results["mappings_created"] += 1

                # Sync subscriptions for this customer
                for sub_id in customer_info.get("subscriptions", []):
                    if sub_id in subscriptions:
                        try:
                            created = _sync_resource_from_subscription(
                                sub_id=sub_id,
                                info=subscriptions[sub_id],
                                offering=offering,
                                project=project,
                                plan=plan,
                                arrow_client=client,
                                period_from=period_from,
                                period_to=period_to,
                            )
                            results["synced"] += 1
                            if created:
                                results["created"] += 1
                                results["orders_created"] += 1
                            else:
                                results["updated"] += 1
                        except Exception as e:
                            logger.warning(f"Failed to sync subscription {sub_id}: {e}")
                            results["errors"].append(
                                {
                                    "subscription_id": sub_id,
                                    "error": str(e),
                                }
                            )

                # Create invoices and invoice items for this customer
                invoice_results = _create_invoices_for_customer(
                    waldur_customer=waldur_customer,
                    mapping=mapping,
                    subscriptions={
                        sub_id: subscriptions[sub_id]
                        for sub_id in customer_info.get("subscriptions", [])
                        if sub_id in subscriptions
                    },
                    price_source=settings.invoice_price_source,
                )
                results["invoices_created"] += invoice_results.get(
                    "invoices_created", 0
                )
                results["invoice_items_created"] += invoice_results.get(
                    "items_created", 0
                )

            except Exception as e:
                logger.warning(f"Failed to process customer {customer_name}: {e}")
                results["errors"].append(
                    {
                        "customer": customer_name,
                        "error": str(e),
                    }
                )
    else:
        # Standard mode: use provided offering and project
        offering = None
        project = None

        if offering_uuid:
            offering = marketplace_models.Offering.objects.filter(
                uuid=offering_uuid
            ).first()
        if project_uuid:
            project = structure_models.Project.objects.filter(uuid=project_uuid).first()

        # Build vendor -> (offering, plan) mapping from ArrowVendorOfferingMapping
        vendor_mapping = {}
        for vom in models.ArrowVendorOfferingMapping.objects.filter(
            settings=settings, is_active=True
        ).select_related("offering", "plan"):
            vendor_mapping[vom.arrow_vendor_name] = (vom.offering, vom.plan)

        for sub_id, info in subscriptions.items():
            try:
                vendor = info.get("vendor", "")
                sub_offering = offering
                sub_plan = None
                if vendor and vendor in vendor_mapping:
                    sub_offering, sub_plan = vendor_mapping[vendor]

                created = _sync_resource_from_subscription(
                    sub_id=sub_id,
                    info=info,
                    offering=sub_offering,
                    project=project,
                    plan=sub_plan,
                    arrow_client=client,
                    period_from=period_from,
                    period_to=period_to,
                )
                results["synced"] += 1
                if created:
                    results["created"] += 1
                    results["orders_created"] += 1
                else:
                    results["updated"] += 1
            except Exception as e:
                logger.warning(f"Failed to sync subscription {sub_id}: {e}")
                results["errors"].append({"subscription_id": sub_id, "error": str(e)})

    logger.info(
        f"Resource sync complete: {results['synced']} synced, "
        f"{results['created']} created, {results['updated']} updated, "
        f"{results['customers_created']} customers created, "
        f"{results['projects_created']} projects created"
    )
    return results


def _get_or_create_arrow_offering(offering_uuid: str = ""):
    """Get or create an offering for Arrow Azure subscriptions with usage-based component."""
    from waldur_core.structure import models as structure_models
    from waldur_mastermind.marketplace import models as marketplace_models

    # Try to use specified offering
    if offering_uuid:
        offering = marketplace_models.Offering.objects.filter(
            uuid=offering_uuid
        ).first()
        if offering:
            _ensure_arrow_offering_component(offering)
            _ensure_arrow_plan(offering)
            return offering

    # Try to find existing Arrow offering
    offering = marketplace_models.Offering.objects.filter(
        name__icontains="Arrow Azure"
    ).first()
    if offering:
        _ensure_arrow_offering_component(offering)
        _ensure_arrow_plan(offering)
        return offering

    # Create new offering
    logger.info("Creating Arrow Azure offering")

    # Get or create category
    category, _ = marketplace_models.Category.objects.get_or_create(
        title="Cloud Infrastructure",
        defaults={"description": "Cloud infrastructure and compute services"},
    )

    # Get or create a customer for the offering (service provider)
    customer = structure_models.Customer.objects.first()
    if not customer:
        customer = structure_models.Customer.objects.create(
            name="Arrow Service Provider",
            abbreviation="ARROW-SP",
        )

    offering = marketplace_models.Offering.objects.create(
        name="Arrow Azure Subscriptions",
        description="Azure subscriptions managed through Arrow (ArrowSphere)",
        category=category,
        customer=customer,
        type="Support.OfferingTemplate",
        state=marketplace_models.Offering.States.ACTIVE,
    )
    logger.info(f"Created offering: {offering.name} ({offering.uuid})")

    # Create usage-based component and plan for cloud cost
    _ensure_arrow_offering_component(offering)
    _ensure_arrow_plan(offering)

    return offering


def _ensure_arrow_offering_component(offering):
    """Ensure the Arrow offering has a usage-based cloud cost component."""
    from waldur_mastermind.marketplace import models as marketplace_models
    from waldur_mastermind.marketplace.enums import BillingTypes

    component_type = "cloud_cost"

    # Check if component already exists
    if offering.components.filter(type=component_type).exists():
        return

    # Create usage-based component
    marketplace_models.OfferingComponent.objects.create(
        offering=offering,
        type=component_type,
        name="Cloud Cost",
        description="Monthly cloud consumption cost from Arrow billing",
        billing_type=BillingTypes.USAGE,
        measured_unit="EUR",
    )
    logger.info(f"Created cloud_cost component for offering {offering.uuid}")


def _ensure_arrow_plan(offering):
    """Ensure the Arrow offering has a usage-based plan with cloud_cost component."""
    from waldur_mastermind.marketplace import models as marketplace_models

    plan = offering.plans.first()
    if plan:
        return plan

    plan = marketplace_models.Plan.objects.create(
        offering=offering,
        name="Arrow Cloud Cost",
        description="Usage-based billing plan for Arrow cloud subscriptions",
        unit_price=0,
    )

    component = offering.components.filter(type="cloud_cost").first()
    if component:
        marketplace_models.PlanComponent.objects.create(
            plan=plan,
            component=component,
            price=1,
            amount=0,
        )

    logger.info(f"Created plan '{plan.name}' for offering {offering.uuid}")
    return plan


def _get_or_create_customer_from_arrow(customer_info: dict):
    """
    Get or create a Waldur Customer from Arrow customer data.

    Returns (customer, created) tuple.
    """
    from waldur_core.structure import models as structure_models

    name = customer_info.get("name", "Unknown Customer")
    arrow_id = customer_info.get("arrow_id", "")

    # Try to find by name first
    customer = structure_models.Customer.objects.filter(name=name).first()
    if customer:
        return customer, False

    # Try to find by abbreviation (Arrow ID)
    if arrow_id:
        customer = structure_models.Customer.objects.filter(
            abbreviation=arrow_id[:12]
        ).first()
        if customer:
            return customer, False

    # Create new customer
    logger.info(f"Creating customer: {name}")

    # Build address with city if available
    address_parts = []
    if customer_info.get("address"):
        address_parts.append(customer_info["address"])
    if customer_info.get("city"):
        address_parts.append(customer_info["city"])
    full_address = ", ".join(address_parts)

    customer = structure_models.Customer.objects.create(
        name=name[:150],
        abbreviation=arrow_id[:12] if arrow_id else name[:12],
        email=customer_info.get("email", "")[:75] or "",
        address=full_address[:300] if full_address else "",
        postal=customer_info.get("postal", "")[:20] or "",
        country=customer_info.get("country", "")[:2] or "",
    )
    logger.info(f"Created customer: {customer.name} ({customer.uuid})")
    return customer, True


def _get_or_create_project_for_customer(customer):
    """
    Get or create a project for Arrow resources under a customer.

    Returns (project, created) tuple.
    """
    from waldur_core.structure import models as structure_models

    # Try to find existing Arrow project
    project = structure_models.Project.objects.filter(
        customer=customer, name__icontains="Arrow"
    ).first()
    if project:
        return project, False

    # Create new project
    logger.info(f"Creating project for customer: {customer.name}")

    project = structure_models.Project.objects.create(
        name="Arrow Azure Subscriptions",
        customer=customer,
        description="Azure subscriptions synced from Arrow",
    )
    logger.info(f"Created project: {project.name} ({project.uuid})")
    return project, True


def _aggregate_subscriptions_for_resources(
    export_data: dict,
    include_customer_details: bool = False,
) -> tuple[dict, dict]:
    """
    Aggregate IAAS billing data by Vendor Subscription ID.

    Args:
        export_data: Raw export data from Arrow
        include_customer_details: If True, also extract customer details for force_import

    Returns tuple:
        - subscriptions: {subscription_id: {name, customer, sell_total, buy_total, periods}}
        - customers: {customer_name: {name, arrow_id, email, address, city, country, subscriptions}}
    """
    headers = export_data.get("headers", [])
    values = export_data.get("values", [])

    cols = {h: i for i, h in enumerate(headers)}

    # Subscription fields
    class_idx = cols.get("Classification", -1)
    vendor_sub_idx = cols.get("Vendor Subscription ID", -1)
    friendly_name_idx = cols.get("Friendly Name", -1)
    sell_price_idx = cols.get("Customer Total Price", -1)
    buy_price_idx = cols.get("Total Wholesale Price", -1)
    customer_idx = cols.get("End User Company Name", -1)
    report_period_idx = cols.get("Report Period", -1)
    description_idx = cols.get("Description", -1)
    vendor_idx = cols.get("Vendor Name", -1)
    offer_name_idx = cols.get("Offer Name", -1)

    # Additional detail fields for richer reports
    service_name_idx = cols.get("Service Name", -1)
    arrow_sku_idx = cols.get("Arrow SKU", -1)
    vendor_sku_idx = cols.get("Vendor SKU", -1)
    billing_cycle_idx = cols.get("Billing Cycle", -1)
    qty_idx = cols.get("Qty", -1)
    unit_price_idx = cols.get("Customer Unit Price", -1)
    bill_from_idx = cols.get("Bill From", -1)
    bill_to_idx = cols.get("Bill To", -1)
    license_ref_idx = cols.get("ARS Subscription ID", -1)

    # Customer detail fields (for force_import)
    customer_id_idx = cols.get("End User Company ID", -1)
    customer_email_idx = cols.get("End User E-mail", -1)
    customer_address_idx = cols.get("End User Address Line1", -1)
    customer_city_idx = cols.get("End User City", -1)
    customer_postal_idx = cols.get("End User Post Code", -1)
    customer_country_idx = cols.get("End User Country Code", -1)

    subscriptions = {}
    customers = {}

    for row in values:
        classification = row[class_idx] if class_idx >= 0 else ""
        if classification != "IAAS":
            continue

        sub_id = row[vendor_sub_idx] if vendor_sub_idx >= 0 else None
        if not sub_id:
            continue

        name = row[friendly_name_idx] if friendly_name_idx >= 0 else "Unknown"
        sell_price = _parse_decimal(row[sell_price_idx] if sell_price_idx >= 0 else 0)
        buy_price = _parse_decimal(row[buy_price_idx] if buy_price_idx >= 0 else 0)
        customer_name = row[customer_idx] if customer_idx >= 0 else "Unknown"
        period = row[report_period_idx] if report_period_idx >= 0 else "Unknown"
        description = row[description_idx] if description_idx >= 0 else ""
        vendor = row[vendor_idx] if vendor_idx >= 0 else ""
        offer = row[offer_name_idx] if offer_name_idx >= 0 else ""

        # Additional detail fields
        service_name = row[service_name_idx] if service_name_idx >= 0 else ""
        arrow_sku = row[arrow_sku_idx] if arrow_sku_idx >= 0 else ""
        vendor_sku = row[vendor_sku_idx] if vendor_sku_idx >= 0 else ""
        billing_cycle = row[billing_cycle_idx] if billing_cycle_idx >= 0 else ""
        qty = row[qty_idx] if qty_idx >= 0 else ""
        unit_price = row[unit_price_idx] if unit_price_idx >= 0 else ""
        bill_from = row[bill_from_idx] if bill_from_idx >= 0 else ""
        bill_to = row[bill_to_idx] if bill_to_idx >= 0 else ""
        license_ref = row[license_ref_idx] if license_ref_idx >= 0 else ""

        # Aggregate subscription
        if sub_id not in subscriptions:
            subscriptions[sub_id] = {
                "name": name,
                "customer": customer_name,
                "vendor": vendor,
                "service_name": service_name,
                "arrow_sku": arrow_sku,
                "vendor_sku": vendor_sku,
                "billing_cycle": billing_cycle,
                "license_reference": license_ref,
                "sell_total": Decimal("0"),
                "buy_total": Decimal("0"),
                "periods": {},
            }

        subscriptions[sub_id]["sell_total"] += sell_price
        subscriptions[sub_id]["buy_total"] += buy_price

        if period not in subscriptions[sub_id]["periods"]:
            subscriptions[sub_id]["periods"][period] = {
                "sell": Decimal("0"),
                "buy": Decimal("0"),
                "bill_from": bill_from,
                "bill_to": bill_to,
                "items": [],
            }

        subscriptions[sub_id]["periods"][period]["sell"] += sell_price
        subscriptions[sub_id]["periods"][period]["buy"] += buy_price
        subscriptions[sub_id]["periods"][period]["items"].append(
            {
                "description": description,
                "offer": offer,
                "service_name": service_name,
                "arrow_sku": arrow_sku,
                "qty": str(qty),
                "unit_price": str(unit_price),
                "sell": str(sell_price),
                "buy": str(buy_price),
            }
        )

        # Aggregate customer details (for force_import)
        if include_customer_details and customer_name not in customers:
            customers[customer_name] = {
                "name": customer_name,
                "arrow_id": row[customer_id_idx] if customer_id_idx >= 0 else "",
                "email": row[customer_email_idx] if customer_email_idx >= 0 else "",
                "address": row[customer_address_idx]
                if customer_address_idx >= 0
                else "",
                "city": row[customer_city_idx] if customer_city_idx >= 0 else "",
                "postal": row[customer_postal_idx] if customer_postal_idx >= 0 else "",
                "country": row[customer_country_idx]
                if customer_country_idx >= 0
                else "",
                "subscriptions": [],
            }

        # Link subscription to customer
        if (
            include_customer_details
            and sub_id not in customers[customer_name]["subscriptions"]
        ):
            customers[customer_name]["subscriptions"].append(sub_id)

    return subscriptions, customers


def _sync_resource_from_subscription(
    sub_id: str,
    info: dict,
    offering=None,
    project=None,
    plan=None,
    arrow_client=None,
    period_from: str = "",
    period_to: str = "",
) -> bool:
    """
    Sync a single Arrow subscription to a Waldur Resource.

    Creates Resource and corresponding Order when creating new resources.
    Fetches monthly consumption data from Arrow if license_reference is available.

    Returns True if resource was created, False if updated.
    """
    from waldur_core.core.models import User
    from waldur_mastermind.marketplace import models as marketplace_models
    from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes

    license_ref = info.get("license_reference", "")

    # Build report - try to fetch consumption data if we have license reference
    if license_ref and arrow_client and period_from:
        report = _build_consumption_report(
            arrow_client=arrow_client,
            license_reference=license_ref,
            period_from=period_from,
            period_to=period_to or period_from,
            info=info,
            sub_id=sub_id,
        )
    else:
        # Fallback to billing export data (still fetch license details if possible)
        report = _build_billing_export_report(sub_id, info, arrow_client=arrow_client)

    # Store license reference in attributes
    attributes = {
        "arrow_license_reference": license_ref,
        "arrow_vendor": info.get("vendor", ""),
        "arrow_service_name": info.get("service_name", ""),
    }

    # Use component type as key for proper billing integration
    current_usages = {
        "cloud_cost": str(info["sell_total"]),
        "arrow_buy_total": str(info["buy_total"]),
    }

    with transaction.atomic():
        # Find existing resource by backend_id
        resource = marketplace_models.Resource.objects.filter(backend_id=sub_id).first()

        if resource:
            # Update existing resource
            resource.name = info["name"]
            resource.report = report
            resource.current_usages = current_usages
            # Merge attributes
            resource.attributes = {**resource.attributes, **attributes}
            resource.save(
                update_fields=[
                    "name",
                    "report",
                    "current_usages",
                    "attributes",
                    "modified",
                ]
            )
            logger.info(f"Updated resource {resource.uuid} for subscription {sub_id}")

            # Create/update ComponentUsage records for each period
            _create_component_usages(resource, info["periods"])

            return False
        elif offering and project:
            # Create new resource with plan
            if not plan:
                plan = offering.plans.first()
            resource = marketplace_models.Resource.objects.create(
                name=info["name"],
                offering=offering,
                project=project,
                plan=plan,
                backend_id=sub_id,
                state=marketplace_models.Resource.States.OK,
                report=report,
                current_usages=current_usages,
                attributes=attributes,
            )
            logger.info(f"Created resource {resource.uuid} for subscription {sub_id}")

            # Create plan period for billing
            if plan:
                marketplace_models.ResourcePlanPeriod.objects.create(
                    resource=resource,
                    plan=plan,
                    start=resource.created,
                    end=None,
                )

            # Create corresponding Order (following import_marketplace_orders pattern)
            staff_user = User.objects.filter(is_staff=True).first()
            if staff_user:
                marketplace_models.Order.objects.create(
                    resource=resource,
                    offering=offering,
                    project=project,
                    type=OrderTypes.CREATE,
                    state=OrderStates.DONE,
                    created_by=staff_user,
                    consumer_reviewed_by=staff_user,
                    consumer_reviewed_at=resource.created,
                    attributes=resource.attributes,
                    limits=resource.limits,
                )
                logger.info(f"Created order for resource {resource.uuid}")
            else:
                logger.warning("No staff user found, skipping order creation")

            # Create ComponentUsage records for each period
            _create_component_usages(resource, info["periods"])

            return True
        else:
            logger.warning(
                f"No existing resource for {sub_id} and no offering/project specified"
            )
            return False


def _build_billing_export_report(
    sub_id: str, info: dict, arrow_client=None
) -> list[dict]:
    """Build report from billing export data (fallback when consumption API unavailable)."""
    report = []

    license_reference = info.get("license_reference")

    # Overview section with billing totals
    margin = info["sell_total"] - info["buy_total"]
    margin_pct = (margin / info["buy_total"] * 100) if info["buy_total"] else 0

    overview_lines = [
        f"Subscription ID: {sub_id}",
        f"Vendor: {info.get('vendor', 'N/A')}",
        f"Service: {info.get('service_name', 'N/A')}",
        f"Billing Cycle: {info.get('billing_cycle', 'N/A')}",
        "",
        f"Arrow SKU: {info.get('arrow_sku', 'N/A')}",
        f"Vendor SKU: {info.get('vendor_sku', 'N/A')}",
        f"License Reference: {license_reference or 'N/A'}",
        "",
        "Billing Totals (selected period):",
        f"  Sell Total: EUR {info['sell_total']:.2f}",
        f"  Buy Total: EUR {info['buy_total']:.2f}",
        f"  Margin: EUR {margin:.2f} ({margin_pct:.1f}%)",
    ]
    report.append(
        {
            "header": "Billing Summary",
            "body": "\n".join(overview_lines),
        }
    )

    # Per-period sections with detailed cost breakdown
    for period, pdata in sorted(info["periods"].items()):
        margin = pdata["sell"] - pdata["buy"]
        margin_pct = (margin / pdata["buy"] * 100) if pdata["buy"] else 0

        body_lines = [
            f"Period: {pdata.get('bill_from', '')} - {pdata.get('bill_to', '')}",
            "",
            "Cost Summary:",
            f"  Sell Price: EUR {pdata['sell']:.2f}",
            f"  Buy Price: EUR {pdata['buy']:.2f}",
            f"  Margin: EUR {margin:.2f} ({margin_pct:.1f}%)",
            "",
            "Consumption Details:",
        ]

        for item in pdata["items"]:
            item_lines = [
                f"  • {item.get('description') or item.get('offer', 'Usage')}",
            ]
            if item.get("service_name"):
                item_lines.append(f"    Service: {item['service_name']}")
            if item.get("arrow_sku"):
                item_lines.append(f"    SKU: {item['arrow_sku']}")
            if item.get("qty") and item.get("unit_price"):
                item_lines.append(
                    f"    Qty: {item['qty']} × EUR {item['unit_price']} = EUR {item['sell']}"
                )
            else:
                item_lines.append(f"    Amount: EUR {item['sell']}")
            body_lines.extend(item_lines)

        report.append(
            {
                "header": f"Billing Period: {period}",
                "body": "\n".join(body_lines),
            }
        )

    return report


def _build_consumption_report(
    arrow_client,
    license_reference: str,
    period_from: str,
    period_to: str,
    info: dict,
    sub_id: str,
) -> list[dict]:
    """
    Build report from Arrow monthly consumption API.

    Fetches detailed consumption data including:
    - Billing summary (sell/buy totals, margin)
    - Vendor Product Name
    - Vendor Meter Category/Sub-Category
    - Unit of Measure
    - Quantities and prices
    """
    report = []

    # Overview section with billing totals
    margin = info["sell_total"] - info["buy_total"]
    margin_pct = (margin / info["buy_total"] * 100) if info["buy_total"] else 0

    overview_lines = [
        f"Subscription ID: {sub_id}",
        f"License Reference: {license_reference}",
        f"Vendor: {info.get('vendor', 'N/A')}",
        "",
        "Billing Totals (selected period):",
        f"  Sell Total: EUR {info['sell_total']:.2f}",
        f"  Buy Total: EUR {info['buy_total']:.2f}",
        f"  Margin: EUR {margin:.2f} ({margin_pct:.1f}%)",
    ]
    report.append(
        {
            "header": "Billing Summary",
            "body": "\n".join(overview_lines),
        }
    )

    # Fetch consumption data from Arrow
    try:
        consumption_data = arrow_client.get_monthly_consumption(
            license_reference=license_reference,
            period_from=period_from,
            period_to=period_to,
        )
        consumption_lines = arrow_client.parse_consumption_to_dicts(consumption_data)
    except Exception as e:
        logger.warning(f"Failed to fetch consumption for {license_reference}: {e}")
        # Fallback to billing export data for per-period sections
        for period, pdata in sorted(info["periods"].items()):
            report.append(_build_period_section_from_billing(period, pdata))
        return report

    if not consumption_lines:
        logger.info(
            f"No consumption data for {license_reference}, using billing export"
        )
        for period, pdata in sorted(info["periods"].items()):
            report.append(_build_period_section_from_billing(period, pdata))
        return report

    # Group consumption by report period
    by_period: dict[str, list[dict]] = {}
    for line in consumption_lines:
        period = line.get("Report Period", "Unknown")
        if period not in by_period:
            by_period[period] = []
        by_period[period].append(line)

    # Build per-period sections from consumption data
    for period in sorted(by_period.keys()):
        lines = by_period[period]

        # Calculate totals for this period
        total_sell = sum(float(line.get("Total sell price", 0) or 0) for line in lines)
        total_buy = sum(float(line.get("Total buy price", 0) or 0) for line in lines)
        margin = total_sell - total_buy
        margin_pct = (margin / total_buy * 100) if total_buy else 0

        # Get billing dates from first line
        first_line = lines[0] if lines else {}
        bill_start = first_line.get("Vendor Billing Start Date", "")
        bill_end = first_line.get("Vendor Billing End Date", "")

        body_lines = [
            f"Billing Period: {bill_start} - {bill_end}",
            "",
            "Cost Summary:",
            f"  Sell Price: EUR {total_sell:.2f}",
            f"  Buy Price: EUR {total_buy:.2f}",
            f"  Margin: EUR {margin:.2f} ({margin_pct:.1f}%)",
            "",
            "Monthly Consumption Details:",
        ]

        # Group by meter category for cleaner display
        by_category: dict[str, list[dict]] = {}
        for line in lines:
            category = line.get("Vendor Meter Category") or "Other"
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(line)

        for category, cat_lines in sorted(by_category.items()):
            cat_total = sum(
                float(line.get("Total sell price", 0) or 0) for line in cat_lines
            )
            body_lines.append(f"\n  {category}: EUR {cat_total:.2f}")

            for line in cat_lines:
                product = line.get("Vendor Product Name") or "Usage"
                sub_cat = line.get("Vendor Meter Sub-Category") or ""
                qty = line.get("Level Chargeable Quantity")
                uom = line.get("UOM") or ""
                sell = float(line.get("Total sell price", 0) or 0)

                desc = f"    • {product}"
                if sub_cat:
                    desc += f" ({sub_cat})"
                body_lines.append(desc)

                if qty and uom:
                    body_lines.append(f"      Quantity: {qty} {uom}")
                body_lines.append(f"      Amount: EUR {sell:.2f}")

        report.append(
            {
                "header": f"Consumption: {period}",
                "body": "\n".join(body_lines),
            }
        )

    return report


def _build_period_section_from_billing(period: str, pdata: dict) -> dict:
    """Build a single period section from billing export data."""
    margin = pdata["sell"] - pdata["buy"]
    margin_pct = (margin / pdata["buy"] * 100) if pdata["buy"] else 0

    body_lines = [
        f"Period: {pdata.get('bill_from', '')} - {pdata.get('bill_to', '')}",
        "",
        "Cost Summary:",
        f"  Sell Price: EUR {pdata['sell']:.2f}",
        f"  Buy Price: EUR {pdata['buy']:.2f}",
        f"  Margin: EUR {margin:.2f} ({margin_pct:.1f}%)",
        "",
        "Consumption Details:",
    ]

    for item in pdata.get("items", []):
        item_lines = [
            f"  • {item.get('description') or item.get('offer', 'Usage')}",
        ]
        if item.get("service_name"):
            item_lines.append(f"    Service: {item['service_name']}")
        if item.get("arrow_sku"):
            item_lines.append(f"    SKU: {item['arrow_sku']}")
        if item.get("qty") and item.get("unit_price"):
            item_lines.append(
                f"    Qty: {item['qty']} × EUR {item['unit_price']} = EUR {item['sell']}"
            )
        else:
            item_lines.append(f"    Amount: EUR {item['sell']}")
        body_lines.extend(item_lines)

    return {
        "header": f"Billing Period: {period}",
        "body": "\n".join(body_lines),
    }


def _create_component_usages(resource, periods: dict):
    """
    Create ComponentUsage records for each billing period.

    This enables proper billing integration by recording usage against
    the cloud_cost component for each month.
    """
    from waldur_mastermind.marketplace import models as marketplace_models
    from waldur_mastermind.marketplace.utils import get_or_create_plan_period

    # Get the cloud_cost component from the offering
    component = resource.offering.components.filter(type="cloud_cost").first()
    if not component:
        logger.warning(
            f"No cloud_cost component found for offering {resource.offering.uuid}"
        )
        return

    for period, pdata in periods.items():
        try:
            # Parse period (YYYY-MM) to get billing_period date
            year, month = map(int, period.split("-"))
            billing_period = date(year, month, 1)
            usage_date = timezone.make_aware(timezone.datetime(year, month, 1, 0, 0, 0))
            plan_period = get_or_create_plan_period(resource, billing_period)

            # Get or create ComponentUsage for this period
            usage, created = marketplace_models.ComponentUsage.objects.update_or_create(
                resource=resource,
                component=component,
                billing_period=billing_period,
                defaults={
                    "usage": pdata["sell"],
                    "date": usage_date,
                    "plan_period": plan_period,
                    "description": f"Arrow cloud cost for {period}",
                },
            )

            if created:
                logger.debug(
                    f"Created ComponentUsage for {resource.name} period {period}: {pdata['sell']}"
                )
            else:
                logger.debug(
                    f"Updated ComponentUsage for {resource.name} period {period}: {pdata['sell']}"
                )

        except (ValueError, KeyError) as e:
            logger.warning(f"Failed to create ComponentUsage for period {period}: {e}")


def _get_or_create_customer_mapping(
    settings,
    arrow_reference: str,
    arrow_company_name: str,
    waldur_customer,
):
    """
    Get or create an ArrowCustomerMapping.

    Returns (mapping, created) tuple.
    """
    # Try to find existing mapping
    mapping = models.ArrowCustomerMapping.objects.filter(
        settings=settings,
        arrow_reference=arrow_reference,
    ).first()

    if mapping:
        return mapping, False

    # Create new mapping
    mapping = models.ArrowCustomerMapping.objects.create(
        settings=settings,
        arrow_reference=arrow_reference[:100],
        arrow_company_name=arrow_company_name[:255],
        waldur_customer=waldur_customer,
        is_active=True,
    )
    logger.info(
        f"Created customer mapping: {arrow_reference} -> {waldur_customer.name}"
    )
    return mapping, True


def _create_invoices_for_customer(
    waldur_customer,
    mapping,
    subscriptions: dict,
    price_source: str = models.PriceSources.SELL,
) -> dict:
    """
    Create invoices and invoice items for a customer's subscriptions.

    Returns dict with counts of created invoices and items.
    """
    results = {"invoices_created": 0, "items_created": 0}

    # Group subscription data by period
    periods = {}
    for sub_id, sub_info in subscriptions.items():
        for period, period_data in sub_info.get("periods", {}).items():
            if period not in periods:
                periods[period] = []
            periods[period].append(
                {
                    "subscription_id": sub_id,
                    "name": sub_info["name"],
                    "vendor": sub_info["vendor"],
                    "sell": period_data["sell"],
                    "buy": period_data["buy"],
                    "items": period_data["items"],
                }
            )

    # Create invoice and items for each period
    for period, period_subs in periods.items():
        try:
            year, month = map(int, period.split("-"))
        except ValueError:
            logger.warning(f"Invalid period format: {period}")
            continue

        # Get or create invoice for this period
        invoice, invoice_created = invoices_models.Invoice.objects.get_or_create(
            customer=waldur_customer,
            year=year,
            month=month,
        )
        if invoice_created:
            results["invoices_created"] += 1

        # Create invoice items for each subscription in this period
        for sub_data in period_subs:
            # Look up resource by subscription ID (license reference)
            resource = marketplace_models.Resource.objects.filter(
                attributes__arrow_license_reference=sub_data["subscription_id"],
                project__customer=waldur_customer,
            ).first()
            project = resource.project if resource else None

            for item in sub_data["items"]:
                sell_price = _parse_decimal(item["sell"])
                buy_price = _parse_decimal(item.get("buy", "0"))
                unit_price = (
                    buy_price if price_source == models.PriceSources.BUY else sell_price
                )
                if unit_price == Decimal("0"):
                    continue

                # Check if item already exists
                existing_item = invoices_models.InvoiceItem.objects.filter(
                    invoice=invoice,
                    details__source="arrow",
                    details__subscription_id=sub_data["subscription_id"],
                    details__description=item["description"][:100],
                ).first()

                if existing_item:
                    continue

                # Create invoice item
                invoices_models.InvoiceItem.objects.create(
                    invoice=invoice,
                    resource=resource,
                    project=project,
                    project_name=project.name if project else "",
                    project_uuid=project.uuid.hex if project else "",
                    name=f"{sub_data['name']} - {item['description']}"[:255],
                    unit_price=unit_price,
                    quantity=Decimal("1"),
                    unit=invoices_models.InvoiceItem.Units.QUANTITY,
                    details={
                        "source": "arrow",
                        "subscription_id": sub_data["subscription_id"],
                        "subscription_name": sub_data["name"],
                        "vendor": sub_data["vendor"],
                        "description": item["description"][:500],
                        "offer": item["offer"],
                    },
                )
                results["items_created"] += 1

        # Update invoice cache
        invoice.update_cache()

    return results
