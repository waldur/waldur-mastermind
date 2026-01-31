import logging
from decimal import Decimal
from typing import cast

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField, transition
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.invoices import models as invoices_models

logger = logging.getLogger(__name__)


class PriceSources:
    SELL = "sell"
    BUY = "buy"

    CHOICES = (
        (SELL, _("Sell price")),
        (BUY, _("Buy price")),
    )


class ArrowSettings(core_models.UuidMixin, TimeStampedModel):
    """
    Stores Arrow API configuration for the Waldur deployment.
    Only one active settings record should exist per deployment.
    """

    class Meta:
        app_label = "waldur_arrow"
        verbose_name = _("Arrow Settings")
        verbose_name_plural = _("Arrow Settings")

    api_url = models.URLField(
        max_length=255,
        help_text=_("Arrow API base URL"),
    )
    api_key = models.CharField(
        max_length=500,
        help_text=_("API Key for authentication"),
    )
    export_type_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Billing export template reference"),
    )
    classification_filter = models.CharField(
        max_length=50,
        default="IAAS",
        help_text=_("Filter for IaaS/SaaS classification"),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Whether this settings record is active"),
    )
    sync_enabled = models.BooleanField(
        default=False,
        help_text=_("Whether automatic billing sync is enabled"),
    )
    partner_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Arrow partner reference (discovered from API)"),
    )
    partner_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Arrow partner name (discovered from API)"),
    )
    invoice_price_source = models.CharField(
        max_length=10,
        choices=PriceSources.CHOICES,
        default=PriceSources.SELL,
        help_text=_("Which price to use for invoice items: sell or buy"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def __str__(self):
        return f"Arrow Settings ({self.api_url})"

    @classmethod
    def get_active(cls) -> "ArrowSettings | None":
        """Get the currently active Arrow settings."""
        return cls.objects.filter(is_active=True).first()


class ArrowVendorOfferingMapping(core_models.UuidMixin, TimeStampedModel):
    """
    Maps Arrow vendor names to Waldur marketplace offerings.
    Each Arrow vendor maps to exactly one Waldur offering.

    This enables explicit linking between Arrow IaaS providers (e.g., Microsoft, AWS)
    and Waldur offerings, allowing separate tracking of Arrow subscriptions
    by cloud provider.
    """

    class Meta:
        app_label = "waldur_arrow"
        verbose_name = _("Arrow Vendor Offering Mapping")
        verbose_name_plural = _("Arrow Vendor Offering Mappings")
        unique_together = ("settings", "arrow_vendor_name")

    settings = models.ForeignKey(
        "ArrowSettings",
        on_delete=models.CASCADE,
        related_name="vendor_offering_mappings",
    )
    arrow_vendor_name = models.CharField(
        max_length=255,
        help_text=_("Arrow vendor name (e.g., 'Microsoft', 'Amazon Web Services')"),
    )
    offering = models.ForeignKey(
        "marketplace.Offering",
        on_delete=models.CASCADE,
        related_name="arrow_vendor_mappings",
        help_text=_("Waldur marketplace offering for this vendor"),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Whether this mapping is active"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def __str__(self):
        return f"{self.arrow_vendor_name} -> {self.offering.name}"


class ArrowCustomerMapping(core_models.UuidMixin, TimeStampedModel):
    """
    Maps Arrow customer references (XSP...) to Waldur Customers.
    """

    class Meta:
        app_label = "waldur_arrow"
        verbose_name = _("Arrow Customer Mapping")
        verbose_name_plural = _("Arrow Customer Mappings")
        unique_together = ("arrow_reference", "settings")

    settings = models.ForeignKey(
        ArrowSettings,
        on_delete=models.CASCADE,
        related_name="customer_mappings",
    )
    arrow_reference = models.CharField(
        max_length=255,
        help_text=_("Arrow customer ID (e.g., 'XSP661245')"),
    )
    arrow_company_name = models.CharField(
        max_length=500,
        blank=True,
        help_text=_("Arrow company name"),
    )
    waldur_customer = models.ForeignKey(
        structure_models.Customer,
        on_delete=models.CASCADE,
        related_name="arrow_mappings",
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Whether this mapping is active"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def __str__(self):
        return f"{self.arrow_reference} -> {self.waldur_customer.name}"


class ArrowBillingSync(core_models.UuidMixin, TimeStampedModel):
    """
    Tracks billing sync state per Arrow statement.
    """

    class States:
        PENDING = 1
        SYNCED = 2
        VALIDATED = 3
        RECONCILED = 4

        CHOICES = (
            (PENDING, _("Pending")),
            (SYNCED, _("Synced")),
            (VALIDATED, _("Validated")),
            (RECONCILED, _("Reconciled")),
        )

    class Meta:
        app_label = "waldur_arrow"
        verbose_name = _("Arrow Billing Sync")
        verbose_name_plural = _("Arrow Billing Syncs")
        unique_together = ("statement_reference", "customer_mapping")

    customer_mapping = models.ForeignKey(
        ArrowCustomerMapping,
        on_delete=models.CASCADE,
        related_name="billing_syncs",
    )
    statement_reference = models.CharField(
        max_length=255,
        help_text=_("Arrow statement ID"),
    )
    report_period = models.CharField(
        max_length=7,
        help_text=_("Report period in YYYY-MM format"),
    )
    arrow_state = models.CharField(
        max_length=50,
        blank=True,
        help_text=_("Arrow billing state (pending/validated)"),
    )
    state = FSMIntegerField(
        default=States.PENDING,
        choices=States.CHOICES,
        help_text=_("Waldur sync state"),
    )
    buy_total = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        default=Decimal("0"),
        help_text=_("Total buy amount"),
    )
    sell_total = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        default=Decimal("0"),
        help_text=_("Total sell amount"),
    )
    currency = models.CharField(
        max_length=10,
        default="EUR",
        help_text=_("Currency code"),
    )
    invoice = models.ForeignKey(
        invoices_models.Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="arrow_syncs",
    )
    error_message = models.TextField(
        blank=True,
        help_text=_("Error message if sync failed"),
    )
    synced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When billing was last synced"),
    )
    validated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When Arrow validated the billing"),
    )
    reconciled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When reconciliation was applied"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def __str__(self):
        return f"Sync {self.statement_reference} ({self.report_period})"

    @transition(
        field=state,
        source=States.PENDING,
        target=States.SYNCED,
    )
    def mark_synced(self):
        """Mark sync as completed."""
        from django.utils import timezone

        self.synced_at = timezone.now()

    @transition(
        field=state,
        source=States.SYNCED,
        target=States.VALIDATED,
    )
    def mark_validated(self):
        """Mark as validated by Arrow."""
        from django.utils import timezone

        self.validated_at = timezone.now()

    @transition(
        field=state,
        source=States.VALIDATED,
        target=States.RECONCILED,
    )
    def mark_reconciled(self):
        """Mark as reconciled (compensations applied)."""
        from django.utils import timezone

        self.reconciled_at = timezone.now()


class ArrowBillingSyncItem(core_models.UuidMixin, TimeStampedModel):
    """
    Links Arrow billing lines to Waldur InvoiceItems.
    """

    class Meta:
        app_label = "waldur_arrow"
        verbose_name = _("Arrow Billing Sync Item")
        verbose_name_plural = _("Arrow Billing Sync Items")
        unique_together = ("arrow_line_reference", "billing_sync")

    billing_sync = models.ForeignKey(
        ArrowBillingSync,
        on_delete=models.CASCADE,
        related_name="items",
    )
    arrow_line_reference = models.CharField(
        max_length=255,
        help_text=_("Arrow line ID"),
    )
    invoice_item = models.ForeignKey(
        invoices_models.InvoiceItem,
        on_delete=models.CASCADE,
        related_name="arrow_sync_items",
    )
    original_price = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        default=Decimal("0"),
        help_text=_("Original price for reconciliation tracking"),
    )
    compensation_item = models.ForeignKey(
        invoices_models.InvoiceItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="arrow_compensation_source",
        help_text=_("Compensation invoice item if adjusted"),
    )
    vendor_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Vendor name (e.g., Microsoft)"),
    )
    subscription_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Arrow subscription reference"),
    )
    classification = models.CharField(
        max_length=50,
        blank=True,
        help_text=_("Classification (IAAS/SAAS)"),
    )
    description = models.TextField(
        blank=True,
        help_text=_("Line item description"),
    )
    quantity = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        default=Decimal("1"),
        help_text=_("Quantity"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def __str__(self):
        return f"Item {self.arrow_line_reference}"

    def get_invoice_item_details(self) -> dict:
        """Generate details dict for InvoiceItem."""
        return {
            "source": "arrow",
            "arrow_line_reference": self.arrow_line_reference,
            "vendor_name": self.vendor_name,
            "subscription_reference": self.subscription_reference,
            "classification": self.classification,
        }

    def get_compensation_details(self, final_price: Decimal) -> dict:
        """Generate details dict for compensation InvoiceItem."""
        return {
            "source": "arrow_reconciliation",
            "original_line_reference": self.arrow_line_reference,
            "original_price": str(self.original_price),
            "final_price": str(final_price),
            "original_period": self.billing_sync.report_period,
        }


class ArrowConsumptionRecord(core_models.UuidMixin, TimeStampedModel):
    """
    Tracks real-time consumption data for reconciliation with billing export.

    This model enables consumption-based billing where:
    1. Hourly/daily consumption data is synced from Arrow's Consumption API
    2. When Arrow's finalized billing export arrives, the difference is reconciled
    3. Compensation items are created for any discrepancies

    Workflow:
    - sync_arrow_consumption task updates consumed_sell/consumed_buy from API
    - check_billing_export task fetches finalized billing and compares
    - If final amounts differ from consumed amounts, compensation is applied
    """

    class Meta:
        app_label = "waldur_arrow"
        verbose_name = _("Arrow Consumption Record")
        verbose_name_plural = _("Arrow Consumption Records")
        unique_together = ("resource", "billing_period", "license_reference")

    # Resource reference
    resource = models.ForeignKey(
        "marketplace.Resource",
        on_delete=models.CASCADE,
        related_name="arrow_consumption_records",
        help_text=_("Waldur marketplace resource"),
    )

    # Arrow identifiers
    license_reference = models.CharField(
        max_length=255,
        help_text=_("Arrow license reference (e.g., 'XSP12345')"),
    )
    # Billing period (first day of month)
    billing_period = models.DateField(
        help_text=_("First day of the billing month"),
    )

    # Consumption amounts from API (provisional)
    consumed_sell = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        default=Decimal("0"),
        help_text=_("Consumed sell amount from Consumption API"),
    )
    consumed_buy = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        default=Decimal("0"),
        help_text=_("Consumed buy amount from Consumption API"),
    )

    # Final amounts from billing export
    final_sell = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        null=True,
        blank=True,
        help_text=_("Final sell amount from billing export"),
    )
    final_buy = models.DecimalField(
        max_digits=common_mixins.PRICE_MAX_DIGITS,
        decimal_places=common_mixins.PRICE_DECIMAL_PLACES,
        null=True,
        blank=True,
        help_text=_("Final buy amount from billing export"),
    )

    # Invoice item references
    invoice_item = models.ForeignKey(
        invoices_models.InvoiceItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="arrow_consumption_records",
        help_text=_("Original provisional invoice item"),
    )
    compensation_item = models.ForeignKey(
        invoices_models.InvoiceItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="arrow_consumption_compensations",
        help_text=_("Compensation invoice item if adjustment was needed"),
    )

    # Tracking timestamps
    last_sync_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When consumption was last synced from API"),
    )
    finalized_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When billing export data arrived"),
    )
    reconciled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When reconciliation was applied"),
    )

    # Additional tracking data
    raw_data = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Raw consumption data for debugging"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def __str__(self):
        return f"Consumption {self.license_reference} ({self.billing_period})"

    @property
    def is_finalized(self) -> bool:
        """Check if billing export data has arrived."""
        return self.finalized_at is not None

    @property
    def is_reconciled(self) -> bool:
        """Check if reconciliation has been applied."""
        return self.reconciled_at is not None

    @property
    def adjustment_amount(self) -> Decimal | None:
        """Calculate adjustment amount if finalized."""
        if self.final_sell is None:
            return None
        return self.final_sell - self.consumed_sell

    def get_invoice_item_details(self) -> dict:
        """Generate details dict for provisional InvoiceItem."""
        return {
            "source": "arrow_consumption",
            "license_reference": self.license_reference,
            "consumed_sell": str(self.consumed_sell),
            "consumed_buy": str(self.consumed_buy),
            "sync_type": "real_time",
        }

    def get_finalized_details(self) -> dict:
        """Generate details dict after finalization."""
        return {
            "source": "arrow_finalized",
            "license_reference": self.license_reference,
            "consumed_sell": str(self.consumed_sell),
            "final_sell": str(self.final_sell) if self.final_sell else None,
            "adjustment": str(self.adjustment_amount)
            if self.adjustment_amount
            else None,
        }

    def get_compensation_details(self) -> dict:
        """Generate details dict for compensation InvoiceItem."""
        return {
            "source": "arrow_reconciliation",
            "original_period": str(self.billing_period),
            "license_reference": self.license_reference,
            "consumed_sell": str(self.consumed_sell),
            "final_sell": str(self.final_sell) if self.final_sell else None,
            "adjustment": str(self.adjustment_amount)
            if self.adjustment_amount
            else None,
            "reconciled_at": self.reconciled_at.isoformat()
            if self.reconciled_at
            else None,
        }
