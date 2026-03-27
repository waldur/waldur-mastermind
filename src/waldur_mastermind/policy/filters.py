import django_filters

from waldur_core.core import filters as core_filters

from . import models


class PolicyFilter(django_filters.FilterSet):
    class Meta:
        fields = []

    scope = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )
    scope_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )


class ProjectEstimatedCostPolicyFilter(PolicyFilter):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__customer__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="scope__customer__uuid"
    )
    project = core_filters.URLFilter(
        view_name="project-detail", field_name="scope__uuid"
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="scope__uuid"
    )
    query = django_filters.CharFilter(method="filter_query")

    class Meta:
        model = models.ProjectEstimatedCostPolicy
        fields = []

    def filter_query(self, queryset, name, value):
        return queryset.filter(scope__name__icontains=value)


class CustomerEstimatedCostPolicyFilter(PolicyFilter):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )

    class Meta(PolicyFilter.Meta):
        model = models.CustomerEstimatedCostPolicy


class CustomerComponentUsagePolicyFilter(PolicyFilter):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )

    class Meta(PolicyFilter.Meta):
        model = models.CustomerComponentUsagePolicy
