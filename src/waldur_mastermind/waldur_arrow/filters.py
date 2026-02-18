import django_filters

from waldur_core.core import filters as core_filters

from . import models


class ArrowSettingsFilter(django_filters.FilterSet):
    """Filter for ArrowSettings."""

    is_active = django_filters.BooleanFilter()
    sync_enabled = django_filters.BooleanFilter()

    class Meta:
        model = models.ArrowSettings
        fields = ("is_active", "sync_enabled")


class ArrowCustomerMappingFilter(django_filters.FilterSet):
    """Filter for ArrowCustomerMapping."""

    settings = core_filters.URLFilter(
        view_name="admin-arrow-settings-detail",
        field_name="settings__uuid",
    )
    settings_uuid = core_filters.RelatedUUIDFilter(
        view_name="admin-arrow-settings-detail", field_name="settings__uuid"
    )
    arrow_reference = django_filters.CharFilter(lookup_expr="icontains")
    arrow_company_name = django_filters.CharFilter(lookup_expr="icontains")
    waldur_customer = core_filters.URLFilter(
        view_name="customer-detail",
        field_name="waldur_customer__uuid",
    )
    waldur_customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="waldur_customer__uuid"
    )
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = models.ArrowCustomerMapping
        fields = (
            "settings",
            "settings_uuid",
            "arrow_reference",
            "arrow_company_name",
            "waldur_customer",
            "waldur_customer_uuid",
            "is_active",
        )


class ArrowVendorOfferingMappingFilter(django_filters.FilterSet):
    """Filter for ArrowVendorOfferingMapping."""

    settings = core_filters.URLFilter(
        view_name="admin-arrow-settings-detail",
        field_name="settings__uuid",
    )
    settings_uuid = core_filters.RelatedUUIDFilter(
        view_name="admin-arrow-settings-detail", field_name="settings__uuid"
    )
    arrow_vendor_name = django_filters.CharFilter(lookup_expr="icontains")
    offering = core_filters.URLFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="offering__uuid",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = models.ArrowVendorOfferingMapping
        fields = (
            "settings",
            "settings_uuid",
            "arrow_vendor_name",
            "offering",
            "offering_uuid",
            "is_active",
        )


class ArrowBillingSyncFilter(django_filters.FilterSet):
    """Filter for ArrowBillingSync."""

    customer_mapping = core_filters.URLFilter(
        view_name="admin-arrow-customer-mapping-detail",
        field_name="customer_mapping__uuid",
    )
    customer_mapping_uuid = core_filters.RelatedUUIDFilter(
        view_name="admin-arrow-customer-mapping-detail",
        field_name="customer_mapping__uuid",
    )
    settings_uuid = core_filters.RelatedUUIDFilter(
        view_name="admin-arrow-settings-detail",
        field_name="customer_mapping__settings__uuid",
    )
    statement_reference = django_filters.CharFilter(lookup_expr="icontains")
    report_period = django_filters.CharFilter(lookup_expr="exact")
    report_period_from = django_filters.CharFilter(
        field_name="report_period",
        lookup_expr="gte",
    )
    report_period_to = django_filters.CharFilter(
        field_name="report_period",
        lookup_expr="lte",
    )
    state = django_filters.NumberFilter()
    arrow_state = django_filters.CharFilter(lookup_expr="iexact")

    class Meta:
        model = models.ArrowBillingSync
        fields = (
            "customer_mapping",
            "customer_mapping_uuid",
            "settings_uuid",
            "statement_reference",
            "report_period",
            "report_period_from",
            "report_period_to",
            "state",
            "arrow_state",
        )


class ArrowConsumptionRecordFilter(django_filters.FilterSet):
    """Filter for ArrowConsumptionRecord."""

    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail",
        field_name="resource__uuid",
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="resource__project__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="resource__project__customer__uuid"
    )
    license_reference = django_filters.CharFilter(lookup_expr="icontains")
    billing_period = django_filters.DateFilter()
    billing_period_from = django_filters.DateFilter(
        field_name="billing_period",
        lookup_expr="gte",
    )
    billing_period_to = django_filters.DateFilter(
        field_name="billing_period",
        lookup_expr="lte",
    )
    is_finalized = django_filters.BooleanFilter(method="filter_is_finalized")
    is_reconciled = django_filters.BooleanFilter(method="filter_is_reconciled")

    class Meta:
        model = models.ArrowConsumptionRecord
        fields = (
            "resource",
            "resource_uuid",
            "project_uuid",
            "customer_uuid",
            "license_reference",
            "billing_period",
            "billing_period_from",
            "billing_period_to",
            "is_finalized",
            "is_reconciled",
        )

    def filter_is_finalized(self, queryset, name, value):
        if value is True:
            return queryset.filter(finalized_at__isnull=False)
        elif value is False:
            return queryset.filter(finalized_at__isnull=True)
        return queryset

    def filter_is_reconciled(self, queryset, name, value):
        if value is True:
            return queryset.filter(reconciled_at__isnull=False)
        elif value is False:
            return queryset.filter(reconciled_at__isnull=True)
        return queryset


class ArrowBillingSyncItemFilter(django_filters.FilterSet):
    """Filter for ArrowBillingSyncItem."""

    billing_sync = core_filters.URLFilter(
        view_name="admin-arrow-billing-sync-detail",
        field_name="billing_sync__uuid",
    )
    billing_sync_uuid = core_filters.RelatedUUIDFilter(
        view_name="admin-arrow-billing-sync-detail", field_name="billing_sync__uuid"
    )
    report_period = django_filters.CharFilter(
        field_name="billing_sync__report_period",
        lookup_expr="exact",
    )
    vendor_name = django_filters.CharFilter(lookup_expr="icontains")
    classification = django_filters.CharFilter(lookup_expr="exact")
    subscription_reference = django_filters.CharFilter(lookup_expr="icontains")
    arrow_line_reference = django_filters.CharFilter(lookup_expr="icontains")
    has_compensation = django_filters.BooleanFilter(method="filter_has_compensation")

    class Meta:
        model = models.ArrowBillingSyncItem
        fields = (
            "billing_sync",
            "billing_sync_uuid",
            "report_period",
            "vendor_name",
            "classification",
            "subscription_reference",
            "arrow_line_reference",
            "has_compensation",
        )

    def filter_has_compensation(self, queryset, name, value):
        if value is True:
            return queryset.filter(compensation_item__isnull=False)
        elif value is False:
            return queryset.filter(compensation_item__isnull=True)
        return queryset
