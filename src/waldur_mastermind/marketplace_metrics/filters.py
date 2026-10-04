import django_filters

from waldur_core.core import filters as core_filters

from . import models


class MetricDefinitionFilter(django_filters.FilterSet):
    key = django_filters.CharFilter(lookup_expr="icontains")
    name = django_filters.CharFilter(lookup_expr="icontains")
    owner_customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="owner_customer__uuid"
    )
    is_global = django_filters.BooleanFilter(
        field_name="owner_customer", lookup_expr="isnull"
    )

    class Meta:
        model = models.MetricDefinition
        fields = ["kind", "state"]


class OfferingMetricFilter(django_filters.FilterSet):
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    definition_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-metric-definition-detail",
        field_name="definition__uuid",
    )
    key = django_filters.CharFilter(field_name="definition__key")

    class Meta:
        model = models.OfferingMetric
        fields = ["state"]


class MetricGoalFilter(django_filters.FilterSet):
    offering_metric_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-offering-metric-detail",
        field_name="offering_metric__uuid",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid"
    )
    is_default = django_filters.BooleanFilter(
        field_name="project", lookup_expr="isnull"
    )

    class Meta:
        model = models.MetricGoal
        fields = ["period"]
