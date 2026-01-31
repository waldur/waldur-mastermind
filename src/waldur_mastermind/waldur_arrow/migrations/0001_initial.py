from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("invoices", "0022_fix_non_billable_offering_invoice_items"),
        ("structure", "0071_customer_user_assurance_levels_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ArrowSettings",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "api_url",
                    models.URLField(help_text="Arrow API base URL", max_length=255),
                ),
                (
                    "api_key",
                    models.CharField(
                        help_text="API Key for authentication", max_length=500
                    ),
                ),
                (
                    "export_type_reference",
                    models.CharField(
                        blank=True,
                        help_text="Billing export template reference",
                        max_length=255,
                    ),
                ),
                (
                    "classification_filter",
                    models.CharField(
                        default="IAAS",
                        help_text="Filter for IaaS/SaaS classification",
                        max_length=50,
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True, help_text="Whether this settings record is active"
                    ),
                ),
                (
                    "sync_enabled",
                    models.BooleanField(
                        default=False,
                        help_text="Whether automatic billing sync is enabled",
                    ),
                ),
                (
                    "partner_reference",
                    models.CharField(
                        blank=True,
                        help_text="Arrow partner reference (discovered from API)",
                        max_length=255,
                    ),
                ),
                (
                    "partner_name",
                    models.CharField(
                        blank=True,
                        help_text="Arrow partner name (discovered from API)",
                        max_length=255,
                    ),
                ),
            ],
            options={
                "verbose_name": "Arrow Settings",
                "verbose_name_plural": "Arrow Settings",
            },
        ),
        migrations.CreateModel(
            name="ArrowCustomerMapping",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "arrow_reference",
                    models.CharField(
                        help_text="Arrow customer ID (e.g., 'XSP661245')",
                        max_length=255,
                    ),
                ),
                (
                    "arrow_company_name",
                    models.CharField(
                        blank=True, help_text="Arrow company name", max_length=500
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        default=True, help_text="Whether this mapping is active"
                    ),
                ),
                (
                    "settings",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="customer_mappings",
                        to="waldur_arrow.arrowsettings",
                    ),
                ),
                (
                    "waldur_customer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="arrow_mappings",
                        to="structure.customer",
                    ),
                ),
            ],
            options={
                "verbose_name": "Arrow Customer Mapping",
                "verbose_name_plural": "Arrow Customer Mappings",
                "unique_together": {("arrow_reference", "settings")},
            },
        ),
        migrations.CreateModel(
            name="ArrowBillingSync",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "statement_reference",
                    models.CharField(help_text="Arrow statement ID", max_length=255),
                ),
                (
                    "report_period",
                    models.CharField(
                        help_text="Report period in YYYY-MM format", max_length=7
                    ),
                ),
                (
                    "arrow_state",
                    models.CharField(
                        blank=True,
                        help_text="Arrow billing state (pending/validated)",
                        max_length=50,
                    ),
                ),
                (
                    "state",
                    django_fsm.FSMIntegerField(
                        choices=[
                            (1, "Pending"),
                            (2, "Synced"),
                            (3, "Validated"),
                            (4, "Reconciled"),
                        ],
                        default=1,
                        help_text="Waldur sync state",
                    ),
                ),
                (
                    "buy_total",
                    models.DecimalField(
                        decimal_places=10,
                        default=Decimal("0"),
                        help_text="Total buy amount",
                        max_digits=22,
                    ),
                ),
                (
                    "sell_total",
                    models.DecimalField(
                        decimal_places=10,
                        default=Decimal("0"),
                        help_text="Total sell amount",
                        max_digits=22,
                    ),
                ),
                (
                    "currency",
                    models.CharField(
                        default="EUR", help_text="Currency code", max_length=10
                    ),
                ),
                (
                    "error_message",
                    models.TextField(
                        blank=True, help_text="Error message if sync failed"
                    ),
                ),
                (
                    "synced_at",
                    models.DateTimeField(
                        blank=True, help_text="When billing was last synced", null=True
                    ),
                ),
                (
                    "validated_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When Arrow validated the billing",
                        null=True,
                    ),
                ),
                (
                    "reconciled_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When reconciliation was applied",
                        null=True,
                    ),
                ),
                (
                    "customer_mapping",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="billing_syncs",
                        to="waldur_arrow.arrowcustomermapping",
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="arrow_syncs",
                        to="invoices.invoice",
                    ),
                ),
            ],
            options={
                "verbose_name": "Arrow Billing Sync",
                "verbose_name_plural": "Arrow Billing Syncs",
                "unique_together": {("statement_reference", "customer_mapping")},
            },
        ),
        migrations.CreateModel(
            name="ArrowBillingSyncItem",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created",
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="created",
                    ),
                ),
                (
                    "modified",
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="modified",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "arrow_line_reference",
                    models.CharField(help_text="Arrow line ID", max_length=255),
                ),
                (
                    "original_price",
                    models.DecimalField(
                        decimal_places=10,
                        default=Decimal("0"),
                        help_text="Original price for reconciliation tracking",
                        max_digits=22,
                    ),
                ),
                (
                    "vendor_name",
                    models.CharField(
                        blank=True,
                        help_text="Vendor name (e.g., Microsoft)",
                        max_length=255,
                    ),
                ),
                (
                    "subscription_reference",
                    models.CharField(
                        blank=True,
                        help_text="Arrow subscription reference",
                        max_length=255,
                    ),
                ),
                (
                    "classification",
                    models.CharField(
                        blank=True,
                        help_text="Classification (IAAS/SAAS)",
                        max_length=50,
                    ),
                ),
                (
                    "description",
                    models.TextField(blank=True, help_text="Line item description"),
                ),
                (
                    "quantity",
                    models.DecimalField(
                        decimal_places=10,
                        default=Decimal("1"),
                        help_text="Quantity",
                        max_digits=22,
                    ),
                ),
                (
                    "billing_sync",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="waldur_arrow.arrowbillingsync",
                    ),
                ),
                (
                    "compensation_item",
                    models.ForeignKey(
                        blank=True,
                        help_text="Compensation invoice item if adjusted",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="arrow_compensation_source",
                        to="invoices.invoiceitem",
                    ),
                ),
                (
                    "invoice_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="arrow_sync_items",
                        to="invoices.invoiceitem",
                    ),
                ),
            ],
            options={
                "verbose_name": "Arrow Billing Sync Item",
                "verbose_name_plural": "Arrow Billing Sync Items",
                "unique_together": {("arrow_line_reference", "billing_sync")},
            },
        ),
    ]
