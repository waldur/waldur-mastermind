import django_filters

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class AllocationFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Allocation
        fields = structure_filters.BaseResourceFilter.Meta.fields + ("is_active",)


class AllocationUserUsageFilter(django_filters.FilterSet):
    allocation = core_filters.URLFilter(
        view_name="slurm-allocation-detail",
        field_name="allocation__uuid",
    )
    allocation_uuid = django_filters.UUIDFilter(field_name="allocation__uuid")

    user = core_filters.URLFilter(view_name="user-detail", field_name="user__uuid")
    user_uuid = django_filters.UUIDFilter(field_name="user__uuid")
    month = django_filters.NumberFilter(field_name="month")
    year = django_filters.NumberFilter(field_name="year")


class AssociationFilter(django_filters.FilterSet):
    allocation = core_filters.URLFilter(
        view_name="slurm-allocation-detail", field_name="allocation__uuid"
    )
    allocation_uuid = django_filters.UUIDFilter(field_name="allocation__uuid")
