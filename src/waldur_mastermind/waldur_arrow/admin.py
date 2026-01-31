from django.contrib import admin

from . import models


class ArrowCustomerMappingInline(admin.TabularInline):
    model = models.ArrowCustomerMapping
    extra = 0
    readonly_fields = ("uuid", "created", "modified")


@admin.register(models.ArrowSettings)
class ArrowSettingsAdmin(admin.ModelAdmin):
    list_display = ("api_url", "partner_name", "is_active", "sync_enabled", "created")
    list_filter = ("is_active", "sync_enabled")
    search_fields = ("api_url", "partner_name", "partner_reference")
    readonly_fields = (
        "uuid",
        "created",
        "modified",
        "partner_reference",
        "partner_name",
    )
    inlines = [ArrowCustomerMappingInline]

    fieldsets = (
        (None, {"fields": ("uuid", "api_url", "api_key")}),
        (
            "Configuration",
            {"fields": ("export_type_reference", "classification_filter")},
        ),
        ("Status", {"fields": ("is_active", "sync_enabled")}),
        (
            "Partner Info",
            {
                "fields": ("partner_reference", "partner_name"),
                "classes": ("collapse",),
            },
        ),
        (
            "Timestamps",
            {
                "fields": ("created", "modified"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(models.ArrowCustomerMapping)
class ArrowCustomerMappingAdmin(admin.ModelAdmin):
    list_display = (
        "arrow_reference",
        "arrow_company_name",
        "waldur_customer",
        "is_active",
        "created",
    )
    list_filter = ("is_active", "settings")
    search_fields = (
        "arrow_reference",
        "arrow_company_name",
        "waldur_customer__name",
    )
    readonly_fields = ("uuid", "created", "modified")
    raw_id_fields = ("waldur_customer",)


class ArrowBillingSyncItemInline(admin.TabularInline):
    model = models.ArrowBillingSyncItem
    extra = 0
    readonly_fields = (
        "uuid",
        "arrow_line_reference",
        "vendor_name",
        "original_price",
        "invoice_item",
        "compensation_item",
    )


@admin.register(models.ArrowBillingSync)
class ArrowBillingSyncAdmin(admin.ModelAdmin):
    list_display = (
        "statement_reference",
        "report_period",
        "customer_mapping",
        "state",
        "arrow_state",
        "sell_total",
        "synced_at",
    )
    list_filter = ("state", "arrow_state", "report_period")
    search_fields = (
        "statement_reference",
        "customer_mapping__arrow_reference",
        "customer_mapping__waldur_customer__name",
    )
    readonly_fields = (
        "uuid",
        "created",
        "modified",
        "synced_at",
        "validated_at",
        "reconciled_at",
    )
    raw_id_fields = ("customer_mapping", "invoice")
    inlines = [ArrowBillingSyncItemInline]


@admin.register(models.ArrowBillingSyncItem)
class ArrowBillingSyncItemAdmin(admin.ModelAdmin):
    list_display = (
        "arrow_line_reference",
        "vendor_name",
        "billing_sync",
        "original_price",
        "has_compensation",
    )
    list_filter = ("classification", "vendor_name")
    search_fields = (
        "arrow_line_reference",
        "vendor_name",
        "subscription_reference",
    )
    readonly_fields = ("uuid", "created", "modified")
    raw_id_fields = ("billing_sync", "invoice_item", "compensation_item")

    def has_compensation(self, obj):
        return obj.compensation_item is not None

    has_compensation.boolean = True
    has_compensation.short_description = "Has Compensation"


@admin.register(models.ArrowConsumptionRecord)
class ArrowConsumptionRecordAdmin(admin.ModelAdmin):
    list_display = (
        "license_reference",
        "resource",
        "billing_period",
        "consumed_sell",
        "final_sell",
        "is_finalized",
        "is_reconciled",
        "last_sync_at",
    )
    list_filter = ("billing_period",)
    search_fields = (
        "license_reference",
        "resource__name",
    )
    readonly_fields = (
        "uuid",
        "created",
        "modified",
        "last_sync_at",
        "finalized_at",
        "reconciled_at",
    )
    raw_id_fields = ("resource", "invoice_item", "compensation_item")

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "uuid",
                    "resource",
                    "license_reference",
                    "billing_period",
                )
            },
        ),
        (
            "Consumption Amounts",
            {
                "fields": ("consumed_sell", "consumed_buy"),
            },
        ),
        (
            "Final Amounts",
            {
                "fields": ("final_sell", "final_buy"),
                "classes": ("collapse",),
            },
        ),
        (
            "Invoice Items",
            {
                "fields": ("invoice_item", "compensation_item"),
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "last_sync_at",
                    "finalized_at",
                    "reconciled_at",
                    "created",
                    "modified",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Raw Data",
            {
                "fields": ("raw_data",),
                "classes": ("collapse",),
            },
        ),
    )

    def is_finalized(self, obj):
        return obj.is_finalized

    is_finalized.boolean = True
    is_finalized.short_description = "Finalized"

    def is_reconciled(self, obj):
        return obj.is_reconciled

    is_reconciled.boolean = True
    is_reconciled.short_description = "Reconciled"
