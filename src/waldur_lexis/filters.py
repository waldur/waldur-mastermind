import django_filters
from django.db.models import Q

from waldur_core.core import filters as core_filters

from . import models


class LexisLinkFilter(django_filters.FilterSet):
    uuid = django_filters.UUIDFilter(field_name="uuid")
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        field_name="robot_account__resource__uuid",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="robot_account__resource__project__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        field_name="robot_account__resource__customer__uuid",
    )
    query = django_filters.CharFilter(
        method="filter_query", label="Filter by robot account username or type"
    )

    class Meta:
        model = models.LexisLink
        fields = ("uuid", "resource_uuid", "project_uuid", "customer_uuid")

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(robot_account__username__icontains=value)
            | Q(robot_account__type__icontains=value)
        )
