import django_filters

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class AllocationFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Allocation
        fields = structure_filters.BaseResourceFilter.Meta.fields + ("is_active",)


class RemoteAllocationFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.RemoteAllocation
        fields = structure_filters.BaseResourceFilter.Meta.fields + ("is_active",)


class AllocationUserUsageFilter(django_filters.FilterSet):
    allocation = core_filters.URLFilter(
        view_name="openportal-allocation-detail",
        field_name="allocation__uuid",
    )
    allocation_uuid = django_filters.UUIDFilter(field_name="allocation__uuid")

    user = core_filters.URLFilter(view_name="user-detail", field_name="user__uuid")
    user_uuid = django_filters.UUIDFilter(field_name="user__uuid")
    month = django_filters.NumberFilter(field_name="month")
    year = django_filters.NumberFilter(field_name="year")


class AssociationFilter(django_filters.FilterSet):
    allocation = core_filters.URLFilter(
        view_name="openportal-allocation-detail", field_name="allocation__uuid"
    )
    allocation_uuid = django_filters.UUIDFilter(field_name="allocation__uuid")


class RemoteAssociationFilter(django_filters.FilterSet):
    allocation = core_filters.URLFilter(
        view_name="openportal-remote-allocation-detail", field_name="allocation__uuid"
    )
    allocation_uuid = django_filters.UUIDFilter(field_name="allocation__uuid")


class UserInfoFilter(django_filters.FilterSet):
    user = core_filters.URLFilter(view_name="user-detail", field_name="user__uuid")
    user_uuid = django_filters.UUIDFilter(field_name="user__uuid")


class ProjectInfoFilter(django_filters.FilterSet):
    project = core_filters.URLFilter(
        view_name="project-detail", field_name="project__uuid"
    )
    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")


class ProjectTemplateFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    portal = django_filters.CharFilter(field_name="portal", lookup_expr="icontains")
    uuid = django_filters.UUIDFilter(field_name="uuid")


class ManagedProjectFilter(django_filters.FilterSet):
    identifier = django_filters.CharFilter(
        field_name="identifier", lookup_expr="icontains"
    )
    local_identifier = django_filters.CharFilter(
        field_name="local_identifier", lookup_expr="icontains"
    )

    project = core_filters.URLFilter(
        view_name="project-detail", field_name="project__uuid"
    )

    project_template = core_filters.URLFilter(
        view_name="openportal-project-template", field_name="project_template__uuid"
    )

    project_uuid = django_filters.UUIDFilter(field_name="project__uuid")
    project_template_uuid = django_filters.UUIDFilter(
        field_name="project_template__uuid"
    )
    state = core_filters.ReviewStateFilter()

    class Meta:
        model = models.ManagedProject
        fields = []
