from rest_framework import serializers

from waldur_core.core import serializers as core_serializers
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import models

# -------------------- Arrow Settings Serializers --------------------


class ArrowCredentialsSerializer(serializers.Serializer):
    """Serializer for validating Arrow API credentials."""

    api_url = serializers.URLField(help_text="Arrow API base URL")
    api_key = serializers.CharField(help_text="Arrow API Key")


class ArrowCredentialsValidationResponseSerializer(serializers.Serializer):
    """Response serializer for credential validation."""

    valid = serializers.BooleanField()
    message = serializers.CharField(required=False)
    error = serializers.CharField(required=False)
    partner_info = serializers.DictField(
        required=False,
        help_text="Raw partner info data from Arrow API",
    )


class ArrowExportTypeSerializer(serializers.Serializer):
    """Serializer for Arrow export type."""

    reference = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True)
    isDefault = serializers.BooleanField(required=False)


class ArrowCustomerDiscoverySerializer(serializers.Serializer):
    """Serializer for discovered Arrow customer."""

    reference = serializers.CharField()
    companyName = serializers.CharField()
    email = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField(required=False, allow_blank=True)
    countryCode = serializers.CharField(required=False, allow_blank=True)


class WaldurCustomerBriefSerializer(serializers.Serializer):
    """Brief serializer for Waldur customer in discovery response."""

    uuid = serializers.UUIDField()
    name = serializers.CharField()
    abbreviation = serializers.CharField(required=False, allow_blank=True)


class CustomerMappingSuggestionSerializer(serializers.Serializer):
    """Serializer for customer mapping suggestion."""

    arrow_customer = ArrowCustomerDiscoverySerializer()
    suggested_waldur_customer = WaldurCustomerBriefSerializer(required=False)
    confidence = serializers.FloatField(required=False)
    existing_mapping = serializers.BooleanField(default=False)


class DiscoverCustomersRequestSerializer(ArrowCredentialsSerializer):
    """Request serializer for customer discovery."""

    pass


class ExportTypeCompatibilitySerializer(serializers.Serializer):
    """Serializer for export type with compatibility info."""

    reference = serializers.CharField()
    name = serializers.CharField()
    required_fields_total = serializers.IntegerField()
    required_fields_found = serializers.IntegerField()
    important_fields_total = serializers.IntegerField()
    important_fields_found = serializers.IntegerField()
    missing_required_fields = serializers.ListField(child=serializers.CharField())
    missing_important_fields = serializers.ListField(child=serializers.CharField())
    compatible = serializers.BooleanField()
    recommended = serializers.BooleanField()


class DiscoverCustomersResponseSerializer(serializers.Serializer):
    """Response serializer for customer discovery."""

    arrow_customers = ArrowCustomerDiscoverySerializer(many=True)
    waldur_customers = WaldurCustomerBriefSerializer(many=True)
    suggestions = CustomerMappingSuggestionSerializer(many=True)
    export_types = ExportTypeCompatibilitySerializer(many=True)


class CustomerMappingInputSerializer(serializers.Serializer):
    """Input serializer for a single customer mapping."""

    arrow_reference = serializers.CharField()
    waldur_customer_uuid = serializers.UUIDField()


class PreviewSettingsRequestSerializer(ArrowCredentialsSerializer):
    """Request serializer for settings preview."""

    export_type_reference = serializers.CharField(required=False, allow_blank=True)
    classification_filter = serializers.CharField(default="IAAS")
    sync_enabled = serializers.BooleanField(default=False)


class PreviewSettingsResponseSerializer(serializers.Serializer):
    """Response serializer for settings preview."""

    api_url = serializers.URLField()
    partner_name = serializers.CharField()
    partner_reference = serializers.CharField()
    export_type_reference = serializers.CharField()
    classification_filter = serializers.CharField()
    sync_enabled = serializers.BooleanField()


class SaveSettingsRequestSerializer(PreviewSettingsRequestSerializer):
    """Request serializer for saving settings."""

    customer_mappings = CustomerMappingInputSerializer(many=True, required=False)


class SaveSettingsResponseSerializer(serializers.Serializer):
    """Response serializer for save settings."""

    settings_uuid = serializers.UUIDField()
    mappings_created = serializers.IntegerField()
    message = serializers.CharField()


# -------------------- Arrow Settings Model Serializer --------------------


class ArrowSettingsSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    """Serializer for ArrowSettings model."""

    url = serializers.HyperlinkedIdentityField(
        view_name="admin-arrow-settings-detail",
        lookup_field="uuid",
    )
    api_key = serializers.CharField(
        required=False,
        help_text="Arrow API Key (leave empty on update to keep current)",
    )

    class Meta:
        model = models.ArrowSettings
        fields = (
            "uuid",
            "url",
            "api_url",
            "api_key",
            "export_type_reference",
            "classification_filter",
            "is_active",
            "sync_enabled",
            "partner_reference",
            "partner_name",
            "invoice_price_source",
            "invoice_item_prefix",
            "created",
            "modified",
        )
        read_only_fields = (
            "uuid",
            "partner_reference",
            "partner_name",
            "created",
            "modified",
        )


class ArrowSettingsCreateSerializer(ArrowSettingsSerializer):
    """Serializer for creating ArrowSettings."""

    api_key = serializers.CharField(
        write_only=True,
        required=True,
        help_text="Arrow API Key (required for creation)",
    )

    class Meta(ArrowSettingsSerializer.Meta):
        pass  # api_key is already in parent's fields


# -------------------- Customer Mapping Serializers --------------------


class ArrowCustomerMappingSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    """Serializer for ArrowCustomerMapping model."""

    url = serializers.HyperlinkedIdentityField(
        view_name="admin-arrow-customer-mapping-detail",
        lookup_field="uuid",
    )
    waldur_customer = serializers.HyperlinkedRelatedField(
        view_name="customer-detail",
        lookup_field="uuid",
        queryset=structure_models.Customer.objects.all(),
    )
    waldur_customer_uuid = serializers.ReadOnlyField(source="waldur_customer.uuid")
    waldur_customer_name = serializers.ReadOnlyField(source="waldur_customer.name")
    settings = serializers.HyperlinkedRelatedField(
        view_name="admin-arrow-settings-detail",
        lookup_field="uuid",
        queryset=models.ArrowSettings.objects.all(),
    )
    settings_uuid = serializers.ReadOnlyField(source="settings.uuid")

    class Meta:
        model = models.ArrowCustomerMapping
        fields = (
            "uuid",
            "url",
            "settings",
            "settings_uuid",
            "arrow_reference",
            "arrow_company_name",
            "waldur_customer",
            "waldur_customer_uuid",
            "waldur_customer_name",
            "is_active",
            "created",
            "modified",
        )
        read_only_fields = (
            "uuid",
            "created",
            "modified",
        )


class ArrowCustomerMappingCreateSerializer(ArrowCustomerMappingSerializer):
    """Serializer for creating customer mappings."""

    waldur_customer = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=structure_models.Customer.objects.all(),
    )
    settings = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ArrowSettings.objects.all(),
    )


# -------------------- Vendor Offering Mapping Serializers --------------------


class ArrowVendorOfferingMappingSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    """Serializer for ArrowVendorOfferingMapping model."""

    url = serializers.HyperlinkedIdentityField(
        view_name="admin-arrow-vendor-offering-mapping-detail",
        lookup_field="uuid",
    )
    settings = serializers.HyperlinkedRelatedField(
        view_name="admin-arrow-settings-detail",
        lookup_field="uuid",
        queryset=models.ArrowSettings.objects.all(),
    )
    settings_uuid = serializers.ReadOnlyField(source="settings.uuid")
    offering_uuid = serializers.ReadOnlyField(source="offering.uuid")
    offering_name = serializers.ReadOnlyField(source="offering.name")
    offering_type = serializers.ReadOnlyField(source="offering.type")
    plan_uuid = serializers.ReadOnlyField(source="plan.uuid")
    plan_name = serializers.ReadOnlyField(source="plan.name")

    class Meta:
        model = models.ArrowVendorOfferingMapping
        fields = (
            "uuid",
            "url",
            "settings",
            "settings_uuid",
            "arrow_vendor_name",
            "offering",
            "offering_uuid",
            "offering_name",
            "offering_type",
            "plan",
            "plan_uuid",
            "plan_name",
            "is_active",
            "created",
            "modified",
        )
        read_only_fields = (
            "uuid",
            "created",
            "modified",
        )

    def get_fields(self):
        fields = super().get_fields()
        # Override offering and plan to use SlugRelatedField so that
        # both create and update accept bare UUIDs instead of hyperlinks.
        fields["offering"] = serializers.SlugRelatedField(
            slug_field="uuid",
            queryset=marketplace_models.Offering.objects.all(),
        )
        fields["plan"] = serializers.SlugRelatedField(
            slug_field="uuid",
            queryset=marketplace_models.Plan.objects.all(),
            required=False,
            allow_null=True,
        )
        return fields


class ArrowVendorOfferingMappingCreateSerializer(ArrowVendorOfferingMappingSerializer):
    """Serializer for creating vendor offering mappings."""

    settings = serializers.SlugRelatedField(
        slug_field="uuid",
        queryset=models.ArrowSettings.objects.all(),
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        plan = attrs.get("plan")
        offering = attrs.get("offering")
        if plan and offering and plan.offering != offering:
            raise serializers.ValidationError(
                {"plan": "Plan must belong to the selected offering."}
            )
        return attrs


class VendorNameChoiceSerializer(serializers.Serializer):
    """Serializer for vendor name dropdown choices."""

    value = serializers.CharField()
    label = serializers.CharField()


class SyncFromArrowRequestSerializer(serializers.Serializer):
    """Request serializer for syncing customers from Arrow."""

    settings_uuid = serializers.UUIDField(required=False)


# -------------------- Billing Sync Serializers --------------------


class ArrowBillingSyncItemSerializer(serializers.ModelSerializer):
    """Serializer for ArrowBillingSyncItem model."""

    invoice_item_uuid = serializers.ReadOnlyField(source="invoice_item.uuid")
    compensation_item_uuid = serializers.ReadOnlyField(source="compensation_item.uuid")

    class Meta:
        model = models.ArrowBillingSyncItem
        fields = (
            "uuid",
            "arrow_line_reference",
            "invoice_item_uuid",
            "original_price",
            "compensation_item_uuid",
            "vendor_name",
            "subscription_reference",
            "classification",
            "description",
            "quantity",
            "created",
        )
        read_only_fields = fields


class ArrowBillingSyncSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    """Serializer for ArrowBillingSync model."""

    url = serializers.HyperlinkedIdentityField(
        view_name="admin-arrow-billing-sync-detail",
        lookup_field="uuid",
    )
    customer_mapping = serializers.HyperlinkedRelatedField(
        view_name="admin-arrow-customer-mapping-detail",
        lookup_field="uuid",
        read_only=True,
    )
    customer_mapping_uuid = serializers.ReadOnlyField(source="customer_mapping.uuid")
    arrow_reference = serializers.ReadOnlyField(
        source="customer_mapping.arrow_reference"
    )
    waldur_customer_name = serializers.ReadOnlyField(
        source="customer_mapping.waldur_customer.name"
    )
    invoice_uuid = serializers.ReadOnlyField(source="invoice.uuid")
    state_display = serializers.SerializerMethodField()
    items = ArrowBillingSyncItemSerializer(many=True, read_only=True)

    class Meta:
        model = models.ArrowBillingSync
        fields = (
            "uuid",
            "url",
            "customer_mapping",
            "customer_mapping_uuid",
            "arrow_reference",
            "waldur_customer_name",
            "statement_reference",
            "report_period",
            "arrow_state",
            "state",
            "state_display",
            "buy_total",
            "sell_total",
            "currency",
            "invoice_uuid",
            "error_message",
            "synced_at",
            "validated_at",
            "reconciled_at",
            "items",
            "created",
            "modified",
        )
        read_only_fields = fields

    def get_state_display(self, obj) -> str:
        return obj.get_state_display()


class TriggerSyncRequestSerializer(serializers.Serializer):
    """Request serializer for triggering billing sync."""

    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    settings_uuid = serializers.UUIDField(required=False)
    resource_uuid = serializers.UUIDField(
        required=False,
        help_text="If set, only sync billing lines for this resource.",
    )


class ReconcileRequestSerializer(serializers.Serializer):
    """Request serializer for reconciliation."""

    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    settings_uuid = serializers.UUIDField(required=False)
    force = serializers.BooleanField(
        default=False,
        help_text="Force reconciliation even if not validated",
    )


def _get_default_period_from():
    """Get default period_from (6 months ago - Arrow's max allowed range)."""
    from datetime import date

    today = date.today()
    year = today.year
    month = today.month - 5  # 6 months total (current + 5 back)
    while month < 1:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


def _get_default_period_to():
    """Get default period_to (current month)."""
    from datetime import date

    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


class SyncResourcesRequestSerializer(serializers.Serializer):
    """Request serializer for syncing Arrow subscriptions to Waldur Resources."""

    period_from = serializers.CharField(
        help_text="Start period in YYYY-MM format (default: 6 months ago, Arrow max)",
        required=False,
    )
    period_to = serializers.CharField(
        help_text="End period in YYYY-MM format (default: current month)",
        required=False,
    )

    def validate(self, attrs):
        if not attrs.get("period_from"):
            attrs["period_from"] = _get_default_period_from()
        if not attrs.get("period_to"):
            attrs["period_to"] = _get_default_period_to()
        return attrs

    settings_uuid = serializers.UUIDField(required=False)
    offering_uuid = serializers.UUIDField(
        required=False,
        help_text="Offering UUID for creating new resources",
    )
    project_uuid = serializers.UUIDField(
        required=False,
        help_text="Project UUID for creating new resources (ignored if force_import=True)",
    )
    force_import = serializers.BooleanField(
        default=False,
        help_text=(
            "If True, auto-create Waldur Customers and Projects from Arrow data. "
            "Each Arrow customer gets a Waldur Customer with an 'Arrow Azure Subscriptions' project."
        ),
    )


class SyncResourcesResponseSerializer(serializers.Serializer):
    """Response serializer for resource sync."""

    synced = serializers.IntegerField()
    created = serializers.IntegerField()
    updated = serializers.IntegerField()


class ArrowSyncErrorSerializer(serializers.Serializer):
    error = serializers.CharField(required=False)
    period = serializers.CharField(required=False)
    subscription_id = serializers.CharField(required=False)
    customer_id = serializers.CharField(required=False)


class ArrowExportTypeSerializer(serializers.Serializer):
    """Serializer for Arrow export type."""

    customers_created = serializers.IntegerField(required=False)
    projects_created = serializers.IntegerField(required=False)
    mappings_created = serializers.IntegerField(required=False)
    invoices_created = serializers.IntegerField(required=False)
    invoice_items_created = serializers.IntegerField(required=False)
    errors = ArrowSyncErrorSerializer(many=True, required=False)


# -------------------- Staff Maintenance Serializers --------------------


class TriggerConsumptionSyncRequestSerializer(serializers.Serializer):
    """Request serializer for triggering consumption sync."""

    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    settings_uuid = serializers.UUIDField(required=False)
    resource_uuid = serializers.UUIDField(
        required=False, help_text="Sync specific resource only"
    )


class CleanupConsumptionRequestSerializer(serializers.Serializer):
    """Request serializer for cleaning up consumption records."""

    period_from = serializers.CharField(required=False, help_text="YYYY-MM format")
    period_to = serializers.CharField(required=False, help_text="YYYY-MM format")
    resource_uuid = serializers.UUIDField(required=False)
    only_finalized = serializers.BooleanField(default=False)
    only_unfinalized = serializers.BooleanField(default=False)
    dry_run = serializers.BooleanField(default=True)

    def validate(self, attrs):
        if attrs.get("only_finalized") and attrs.get("only_unfinalized"):
            raise serializers.ValidationError(
                "Cannot specify both only_finalized and only_unfinalized"
            )
        return attrs


class SyncResourceHistoricalConsumptionRequestSerializer(serializers.Serializer):
    """Request serializer for syncing historical consumption for a specific resource."""

    resource_uuid = serializers.UUIDField(help_text="UUID of the resource to sync")
    period_from = serializers.CharField(
        required=False,
        help_text="Start period in YYYY-MM format. Defaults to 12 months ago.",
    )
    period_to = serializers.CharField(
        required=False,
        help_text="End period in YYYY-MM format. Defaults to current month.",
    )
    force = serializers.BooleanField(
        default=False,
        help_text="If True, sync even for finalized periods.",
    )
    dry_run = serializers.BooleanField(
        default=False,
        help_text="If True, preview consumption data without saving.",
    )

    def validate_period_from(self, value):
        if value:
            try:
                year, month = map(int, value.split("-"))
                if not (1 <= month <= 12 and 2000 <= year <= 2100):
                    raise ValueError
            except (ValueError, AttributeError):
                raise serializers.ValidationError(
                    "Invalid period format. Use YYYY-MM (e.g., 2024-01)"
                )
        return value

    def validate_period_to(self, value):
        if value:
            try:
                year, month = map(int, value.split("-"))
                if not (1 <= month <= 12 and 2000 <= year <= 2100):
                    raise ValueError
            except (ValueError, AttributeError):
                raise serializers.ValidationError(
                    "Invalid period format. Use YYYY-MM (e.g., 2024-12)"
                )
        return value


class PreviewPeriodSerializer(serializers.Serializer):
    period = serializers.CharField()
    status = serializers.CharField(required=False)
    row_count = serializers.IntegerField(required=False)


class SyncResourceHistoricalConsumptionResponseSerializer(serializers.Serializer):
    """Response serializer for historical consumption sync."""

    resource_uuid = serializers.UUIDField()
    resource_name = serializers.CharField()
    periods_synced = serializers.IntegerField()
    periods_skipped = serializers.IntegerField()
    periods_no_data = serializers.IntegerField(default=0)
    errors = ArrowSyncErrorSerializer(many=True, required=False, default=list)
    dry_run = serializers.BooleanField(default=False)
    preview_periods = PreviewPeriodSerializer(many=True, required=False, default=list)


class SyncStatsResponseSerializer(serializers.Serializer):
    """Response serializer for sync actions."""

    resource_uuid = serializers.UUIDField()
    resource_name = serializers.CharField()
    periods_synced = serializers.IntegerField()
    periods_skipped = serializers.IntegerField()
    periods_no_data = serializers.IntegerField(default=0)
    errors = ArrowSyncErrorSerializer(many=True, required=False, default=list)
    dry_run = serializers.BooleanField(default=False)
    preview_periods = PreviewPeriodSerializer(many=True, required=False, default=list)


class SyncPauseRequestSerializer(serializers.Serializer):
    """Request serializer for pausing/resuming sync."""

    settings_uuid = serializers.UUIDField(required=False)
    pause_global = serializers.BooleanField(default=False)


class CleanupConsumptionResponseSerializer(serializers.Serializer):
    """Response serializer for consumption cleanup."""

    dry_run = serializers.BooleanField()
    records_to_delete = serializers.IntegerField()
    records_deleted = serializers.IntegerField()
    compensation_items_affected = serializers.IntegerField()
    invoice_items_affected = serializers.IntegerField()


class ConsumptionStatusResponseSerializer(serializers.Serializer):
    """Response serializer for consumption sync status."""

    global_sync_enabled = serializers.BooleanField()
    settings_sync_enabled = serializers.BooleanField()
    settings_uuid = serializers.UUIDField(allow_null=True)
    last_sync_run = serializers.DateTimeField(allow_null=True)


class PeriodBreakdownSerializer(serializers.Serializer):
    """Serializer for period breakdown in statistics."""

    period = serializers.CharField()
    count = serializers.IntegerField()
    consumed_sell = serializers.DecimalField(max_digits=20, decimal_places=2)
    finalized_count = serializers.IntegerField()
    reconciled_count = serializers.IntegerField()


class ConsumptionStatisticsResponseSerializer(serializers.Serializer):
    """Response serializer for consumption statistics."""

    total_records = serializers.IntegerField()
    pending_records = serializers.IntegerField()
    finalized_records = serializers.IntegerField()
    reconciled_records = serializers.IntegerField()
    total_consumed_sell = serializers.DecimalField(max_digits=20, decimal_places=2)
    total_adjustments = serializers.DecimalField(max_digits=20, decimal_places=2)
    period_breakdown = PeriodBreakdownSerializer(many=True)


class PendingRecordSerializer(serializers.Serializer):
    """Serializer for pending consumption records."""

    uuid = serializers.UUIDField()
    resource_uuid = serializers.UUIDField()
    resource_name = serializers.CharField()
    license_reference = serializers.CharField()
    billing_period = serializers.DateField()
    consumed_sell = serializers.DecimalField(max_digits=20, decimal_places=2)
    last_sync_at = serializers.DateTimeField(allow_null=True)


# -------------------- Raw Arrow API Fetch Serializers --------------------


class FetchConsumptionRequestSerializer(serializers.Serializer):
    """Request serializer for fetching raw consumption from Arrow API."""

    license_reference = serializers.CharField()
    period = serializers.CharField(help_text="YYYY-MM format")


class FetchBillingExportRequestSerializer(serializers.Serializer):
    """Request serializer for fetching raw billing export from Arrow API."""

    period_from = serializers.CharField(help_text="YYYY-MM format")
    period_to = serializers.CharField(help_text="YYYY-MM format")
    classification = serializers.CharField(required=False)


class FetchLicenseInfoRequestSerializer(serializers.Serializer):
    """Request serializer for fetching license info from Arrow API."""

    license_reference = serializers.CharField()


class SyncPauseResponseSerializer(serializers.Serializer):
    """Response serializer for pause/resume sync actions."""

    paused = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="List of paused items"
    )
    resumed = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="List of resumed items",
    )


class FetchConsumptionResponseSerializer(serializers.Serializer):
    """Response serializer for fetch_consumption action."""

    license_reference = serializers.CharField()
    period = serializers.CharField()
    row_count = serializers.IntegerField()
    data = serializers.ListField(
        child=serializers.DictField(),
        help_text="Raw consumption data from Arrow API",
    )


class FetchBillingExportResponseSerializer(serializers.Serializer):
    """Response serializer for fetch_billing_export action."""

    period_from = serializers.CharField()
    period_to = serializers.CharField()
    classification = serializers.CharField()
    row_count = serializers.IntegerField()
    data = serializers.ListField(
        child=serializers.DictField(),
        help_text="Raw billing export data from Arrow API",
    )


class FetchLicenseInfoResponseSerializer(serializers.Serializer):
    """Response serializer for fetch_license_info action."""

    data = serializers.DictField(help_text="Raw license data from Arrow API")


class PendingRecordsResponseSerializer(serializers.Serializer):
    """Response serializer for pending_records action - wraps list of records."""

    records = PendingRecordSerializer(many=True)


# -------------------- Arrow Consumption Record Serializers --------------------


class ArrowConsumptionRecordSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    """Serializer for ArrowConsumptionRecord model."""

    url = serializers.HyperlinkedIdentityField(
        view_name="admin-arrow-consumption-record-detail",
        lookup_field="uuid",
    )
    resource = serializers.HyperlinkedRelatedField(
        view_name="marketplace-resource-detail",
        lookup_field="uuid",
        read_only=True,
    )
    resource_uuid = serializers.ReadOnlyField(source="resource.uuid")
    resource_name = serializers.ReadOnlyField(source="resource.name")
    project_uuid = serializers.ReadOnlyField(source="resource.project.uuid")
    project_name = serializers.ReadOnlyField(source="resource.project.name")
    customer_uuid = serializers.ReadOnlyField(source="resource.project.customer.uuid")
    customer_name = serializers.ReadOnlyField(source="resource.project.customer.name")
    invoice_item_uuid = serializers.ReadOnlyField(source="invoice_item.uuid")
    compensation_item_uuid = serializers.ReadOnlyField(source="compensation_item.uuid")
    is_finalized = serializers.BooleanField(read_only=True)
    is_reconciled = serializers.BooleanField(read_only=True)
    adjustment_amount = serializers.DecimalField(
        max_digits=20, decimal_places=2, read_only=True, allow_null=True
    )

    class Meta:
        model = models.ArrowConsumptionRecord
        fields = (
            "uuid",
            "url",
            "resource",
            "resource_uuid",
            "resource_name",
            "project_uuid",
            "project_name",
            "customer_uuid",
            "customer_name",
            "license_reference",
            "billing_period",
            "consumed_sell",
            "consumed_buy",
            "final_sell",
            "final_buy",
            "invoice_item_uuid",
            "compensation_item_uuid",
            "last_sync_at",
            "finalized_at",
            "reconciled_at",
            "is_finalized",
            "is_reconciled",
            "adjustment_amount",
            "raw_data",
            "created",
            "modified",
        )
        read_only_fields = fields


# -------------------- Extended Billing Sync Item Serializer --------------------


class ArrowBillingSyncItemDetailSerializer(serializers.HyperlinkedModelSerializer):
    """Extended serializer for ArrowBillingSyncItem with related data."""

    url = serializers.HyperlinkedIdentityField(
        view_name="admin-arrow-billing-sync-item-detail",
        lookup_field="uuid",
    )
    billing_sync = serializers.HyperlinkedRelatedField(
        view_name="admin-arrow-billing-sync-detail",
        lookup_field="uuid",
        read_only=True,
    )
    billing_sync_uuid = serializers.ReadOnlyField(source="billing_sync.uuid")
    report_period = serializers.ReadOnlyField(source="billing_sync.report_period")
    invoice_item_uuid = serializers.ReadOnlyField(source="invoice_item.uuid")
    compensation_item_uuid = serializers.ReadOnlyField(source="compensation_item.uuid")
    has_compensation = serializers.SerializerMethodField()

    class Meta:
        model = models.ArrowBillingSyncItem
        fields = (
            "uuid",
            "url",
            "billing_sync",
            "billing_sync_uuid",
            "report_period",
            "arrow_line_reference",
            "invoice_item_uuid",
            "original_price",
            "compensation_item_uuid",
            "has_compensation",
            "vendor_name",
            "subscription_reference",
            "classification",
            "description",
            "quantity",
            "created",
        )
        read_only_fields = fields

    def get_has_compensation(self, obj) -> bool:
        return obj.compensation_item is not None


# -------------------- Customer Billing Summary Serializers --------------------


class CustomerBillingSummaryConsumptionRecordSerializer(serializers.Serializer):
    """Simplified consumption record for billing summary."""

    uuid = serializers.UUIDField()
    license_reference = serializers.CharField()
    resource_name = serializers.CharField(allow_null=True)
    billing_period = serializers.DateField()
    consumed_sell = serializers.DecimalField(max_digits=20, decimal_places=2)
    final_sell = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )
    is_finalized = serializers.BooleanField()
    is_reconciled = serializers.BooleanField()


class CustomerBillingSummaryBillingSyncSerializer(serializers.Serializer):
    """Simplified billing sync for billing summary."""

    uuid = serializers.UUIDField()
    report_period = serializers.CharField()
    state = serializers.CharField()
    sell_total = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )
    items_count = serializers.IntegerField()
    created = serializers.DateTimeField()


class CustomerBillingSummaryResponseSerializer(serializers.Serializer):
    """Response serializer for customer billing summary."""

    customer_mapping_uuid = serializers.UUIDField()
    arrow_reference = serializers.CharField()
    arrow_company_name = serializers.CharField()
    waldur_customer_uuid = serializers.UUIDField()
    waldur_customer_name = serializers.CharField()

    # Summary statistics
    total_consumption_records = serializers.IntegerField()
    total_consumed_sell = serializers.DecimalField(max_digits=20, decimal_places=2)
    total_final_sell = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )
    pending_records = serializers.IntegerField()
    finalized_records = serializers.IntegerField()
    reconciled_records = serializers.IntegerField()

    total_billing_syncs = serializers.IntegerField()
    total_billing_sell = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )

    # Recent records (limited)
    recent_consumption_records = CustomerBillingSummaryConsumptionRecordSerializer(
        many=True
    )
    recent_billing_syncs = CustomerBillingSummaryBillingSyncSerializer(many=True)


class ArrowBillingLineSerializer(serializers.Serializer):
    """Serializer for a billing line from Arrow API."""

    vendor_name = serializers.CharField(allow_blank=True)
    subscription_reference = serializers.CharField(allow_blank=True)
    license_reference = serializers.CharField(
        allow_blank=True,
        help_text="Arrow license reference. Used to fetch consumption data.",
    )
    offer_sku = serializers.CharField(allow_blank=True)
    classification = serializers.CharField(allow_blank=True)
    quantity = serializers.DecimalField(
        max_digits=20, decimal_places=4, allow_null=True
    )
    sell_price = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )
    buy_price = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )


class ArrowConsumptionLineSerializer(serializers.Serializer):
    """Serializer for a consumption line from Arrow API."""

    license_reference = serializers.CharField(
        help_text="Arrow license reference (same as resource backend_id)."
    )
    resource_name = serializers.CharField(allow_blank=True, allow_null=True)
    resource_uuid = serializers.CharField(
        allow_blank=True,
        allow_null=True,
        help_text="UUID of the Waldur resource.",
    )
    period = serializers.CharField()
    sell_price = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )
    buy_price = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )
    error = serializers.CharField(
        allow_blank=True,
        allow_null=True,
        required=False,
        help_text="Error message if fetch failed.",
    )


class FetchCustomerArrowDataResponseSerializer(serializers.Serializer):
    """
    Response serializer for fetching fresh Arrow data for a customer.

    Resource linking: Resources are linked to Arrow via their `backend_id` field
    which should contain the Arrow License Reference (e.g., XSP12345).

    This endpoint works by:
    1. Fetching billing export from Arrow for the current period (for display)
    2. Filtering billing lines by customer reference
    3. Finding Waldur resources that have a backend_id set
    4. Using backend_id directly as license_reference to fetch consumption data

    The backend_id IS the Arrow license reference - no lookup/mapping needed.
    """

    customer_mapping_uuid = serializers.UUIDField()
    arrow_reference = serializers.CharField()
    arrow_company_name = serializers.CharField()
    waldur_customer_name = serializers.CharField()
    period = serializers.CharField()

    # Billing data from Arrow
    billing_available = serializers.BooleanField()
    billing_lines = ArrowBillingLineSerializer(many=True)
    billing_total_sell = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )
    billing_total_buy = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )

    # Consumption data
    consumption_lines = ArrowConsumptionLineSerializer(many=True)
    consumption_total_sell = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )
    consumption_total_buy = serializers.DecimalField(
        max_digits=20, decimal_places=2, allow_null=True
    )

    # Diagnostic info
    total_customer_resources = serializers.IntegerField(
        help_text="Total number of resources for this customer in Waldur."
    )
    resources_with_backend_id = serializers.IntegerField(
        help_text="Number of resources with backend_id set (Arrow license reference)."
    )
    matched_resources = serializers.IntegerField(
        help_text="Number of resources for which consumption was successfully fetched."
    )

    error = serializers.CharField(allow_null=True, allow_blank=True)


# -------------------- License Discovery Serializers --------------------


class ArrowLicenseSerializer(serializers.Serializer):
    """Serializer for an Arrow license from billing export."""

    license_reference = serializers.CharField(
        help_text="Arrow license reference (e.g., XSP12345). Use this as resource backend_id."
    )
    vendor_name = serializers.CharField(allow_blank=True)
    offer_name = serializers.CharField(allow_blank=True)
    offer_sku = serializers.CharField(allow_blank=True)
    friendly_name = serializers.CharField(allow_blank=True)


class WaldurResourceForLinkingSerializer(serializers.Serializer):
    """Serializer for a Waldur resource that can be linked to Arrow."""

    uuid = serializers.UUIDField()
    name = serializers.CharField()
    backend_id = serializers.CharField(
        allow_blank=True,
        help_text="Current backend_id (Arrow license reference if linked).",
    )
    project_name = serializers.CharField(allow_blank=True)
    offering_name = serializers.CharField(allow_blank=True)
    state = serializers.CharField()


class LicenseSuggestionSerializer(serializers.Serializer):
    """Serializer for a suggested resource-to-license match."""

    resource_uuid = serializers.UUIDField()
    resource_name = serializers.CharField()
    license_reference = serializers.CharField()
    license_name = serializers.CharField(allow_blank=True)
    confidence = serializers.FloatField(
        help_text="Confidence score (0-1) based on name similarity."
    )


class DiscoverLicensesResponseSerializer(serializers.Serializer):
    """Response serializer for discovering Arrow licenses and linkable resources."""

    customer_mapping_uuid = serializers.UUIDField()
    arrow_reference = serializers.CharField()
    waldur_customer_name = serializers.CharField()
    arrow_licenses = ArrowLicenseSerializer(
        many=True, help_text="Arrow licenses from billing export for this customer."
    )
    waldur_resources = WaldurResourceForLinkingSerializer(
        many=True, help_text="Waldur resources for this customer."
    )
    suggestions = LicenseSuggestionSerializer(
        many=True, help_text="Suggested matches based on name similarity."
    )
    error = serializers.CharField(allow_null=True, allow_blank=True)


class LinkResourceRequestSerializer(serializers.Serializer):
    """Request serializer for linking a resource to an Arrow license."""

    resource_uuid = serializers.UUIDField(
        help_text="UUID of the Waldur resource to link."
    )
    license_reference = serializers.CharField(
        help_text="Arrow license reference to set as backend_id (e.g., XSP12345)."
    )


class LinkResourceResponseSerializer(serializers.Serializer):
    """Response serializer for linking a resource to an Arrow license."""

    resource_uuid = serializers.UUIDField()
    resource_name = serializers.CharField()
    license_reference = serializers.CharField()
    previous_backend_id = serializers.CharField(allow_blank=True)
    success = serializers.BooleanField()


class ImportLicenseRequestSerializer(serializers.Serializer):
    """Request serializer for importing an Arrow license as a new resource."""

    license_reference = serializers.CharField(
        help_text="Arrow license reference (e.g., XSP12345). Will be set as backend_id."
    )
    license_name = serializers.CharField(
        required=False,
        help_text="Name for the new resource. Defaults to license_reference if not provided.",
    )
    offering_uuid = serializers.UUIDField(
        help_text="UUID of the Waldur offering to create the resource under."
    )
    project_uuid = serializers.UUIDField(
        help_text="UUID of the project to create the resource in."
    )


class ImportLicenseResponseSerializer(serializers.Serializer):
    """Response serializer for importing an Arrow license."""

    resource_uuid = serializers.UUIDField()
    resource_name = serializers.CharField()
    license_reference = serializers.CharField()
    offering_name = serializers.CharField()
    project_name = serializers.CharField()
    success = serializers.BooleanField()


# -------------------- Available Arrow Customers Serializers --------------------


class AvailableArrowCustomersResponseSerializer(serializers.Serializer):
    """
    Response serializer for available Arrow customers endpoint.

    Returns unmapped Arrow customers with suggestions for Waldur organization matches.
    """

    settings_uuid = serializers.UUIDField()
    arrow_customers = ArrowCustomerDiscoverySerializer(many=True)
    waldur_customers = WaldurCustomerBriefSerializer(many=True)
    suggestions = CustomerMappingSuggestionSerializer(many=True)
