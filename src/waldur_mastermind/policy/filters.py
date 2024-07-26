import django_filters

from waldur_core.core import filters as core_filters

from . import models


class EstimatedCostPolicyFilter(django_filters.FilterSet):
    class Meta:
        fields = []

    scope = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )
    scope_uuid = django_filters.UUIDFilter(field_name="scope__uuid")


class ProjectEstimatedCostPolicyFilter(EstimatedCostPolicyFilter):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__customer__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="scope__customer__uuid")
    project = core_filters.URLFilter(
        view_name="project-detail", field_name="scope__uuid"
    )
    project_uuid = django_filters.UUIDFilter(field_name="scope__uuid")

    class Meta:
        model = models.ProjectEstimatedCostPolicy
        fields = []


class CustomerEstimatedCostPolicyFilter(EstimatedCostPolicyFilter):
    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="scope__uuid")

    class Meta(EstimatedCostPolicyFilter.Meta):
        model = models.CustomerEstimatedCostPolicy


class OfferingEstimatedCostPolicyFilter(EstimatedCostPolicyFilter):
    class Meta(EstimatedCostPolicyFilter.Meta):
        model = models.OfferingEstimatedCostPolicy
