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
    # A policy with `resource` set measures only that resource's invoice items,
    # so it belongs to that resource rather than to a project-wide view. Exact
    # match, like the scope filters above: a project-wide policy has
    # resource=null and is deliberately not returned by a resource query.
    resource = core_filters.URLFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail", field_name="resource__uuid"
    )
    # The complement, so a project-wide view can ask for just the policies it
    # can honestly plot rather than fetching all of them and discarding the
    # resource-scoped rows client-side.
    has_resource = django_filters.BooleanFilter(
        field_name="resource", lookup_expr="isnull", exclude=True
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
