import django_filters

from waldur_core.core import filters as core_filters

from . import models


class ProjectEstimatedCostPolicyFilter(django_filters.FilterSet):
    class Meta:
        model = models.ProjectEstimatedCostPolicy
        fields = []

    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__customer__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="scope__customer__uuid")
    project = core_filters.URLFilter(
        view_name="project-detail", field_name="scope__uuid"
    )
    project_uuid = django_filters.UUIDFilter(field_name="scope__uuid")


class CustomerEstimatedCostPolicyFilter(django_filters.FilterSet):
    class Meta:
        model = models.CustomerEstimatedCostPolicy
        fields = []

    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="scope__uuid"
    )
    customer_uuid = django_filters.UUIDFilter(field_name="scope__uuid")
