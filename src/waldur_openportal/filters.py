import django_filters
from django.db.models import Q

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models

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
    allocation_uuid = core_filters.RelatedUUIDFilter(
        view_name="openportal-allocation-detail", field_name="allocation__uuid"
    )

    user = core_filters.URLFilter(view_name="user-detail", field_name="user__uuid")
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid"
    )
    month = django_filters.NumberFilter(field_name="month")
    year = django_filters.NumberFilter(field_name="year")


class AssociationFilter(django_filters.FilterSet):
    allocation = core_filters.URLFilter(
        view_name="openportal-allocation-detail", field_name="allocation__uuid"
    )
    allocation_uuid = core_filters.RelatedUUIDFilter(
        view_name="openportal-allocation-detail", field_name="allocation__uuid"
    )


class RemoteAssociationFilter(django_filters.FilterSet):
    allocation = core_filters.URLFilter(
        view_name="openportal-remote-allocation-detail", field_name="allocation__uuid"
    )
    allocation_uuid = core_filters.RelatedUUIDFilter(
        view_name="openportal-allocation-detail", field_name="allocation__uuid"
    )


class UserInfoFilter(django_filters.FilterSet):
    user = core_filters.URLFilter(view_name="user-detail", field_name="user__uuid")
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid"
    )


class ProjectInfoFilter(django_filters.FilterSet):
    project = core_filters.URLFilter(
        view_name="project-detail", field_name="project__uuid"
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid"
    )


class ProjectTemplateFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    portal = django_filters.CharFilter(field_name="portal", lookup_expr="icontains")
    uuid = django_filters.UUIDFilter(field_name="uuid")


def _identifiers_for_project_uuid(value):
    """Return the set of OpenPortal project_identifier strings for a project UUID.

    Combines two sources:
    1. Allocation.backend_id — covers active projects with existing allocations.
       Available without OpenPortal config.
    2. {projectinfo.shortname}.{portal} — covers cases where the allocation
       has been deleted (e.g. soft-deleted projects, or allocations removed).
       Requires OpenPortal config to resolve the portal name; skipped when
       the plugin is enabled but config is unavailable.
    """
    from . import op as openportal

    allocation_identifiers = set(
        models.Allocation.objects.filter(project__uuid=value)
        .exclude(backend_id="")
        .values_list("backend_id", flat=True)
    )
    if not openportal.ensure_config_loaded():
        return allocation_identifiers
    portal = str(openportal.get_portal())
    shortnames = (
        models.ProjectInfo.objects.filter(
            project__uuid=value,
            shortname__isnull=False,
        )
        .exclude(shortname="")
        .values_list("shortname", flat=True)
    )
    shortname_identifiers = {f"{sn}.{portal}" for sn in shortnames}
    return allocation_identifiers | shortname_identifiers


class CachedProjectUsageReportFilter(django_filters.FilterSet):
    year = django_filters.NumberFilter(field_name="year")
    month = django_filters.NumberFilter(field_name="month")
    project_identifier = django_filters.CharFilter(field_name="project_identifier")
    resource = django_filters.CharFilter(field_name="resource")
    is_complete = django_filters.BooleanFilter(field_name="is_complete")
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", method="filter_by_project_uuid"
    )

    def filter_by_project_uuid(self, queryset, name, value):
        return queryset.filter(
            project_identifier__in=_identifiers_for_project_uuid(value)
        )

    class Meta:
        model = models.CachedProjectUsageReport
        fields = []


class CachedProjectStorageReportFilter(django_filters.FilterSet):
    year = django_filters.NumberFilter(field_name="year")
    month = django_filters.NumberFilter(field_name="month")
    project_identifier = django_filters.CharFilter(field_name="project_identifier")
    resource = django_filters.CharFilter(field_name="resource")
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", method="filter_by_project_uuid"
    )

    def filter_by_project_uuid(self, queryset, name, value):
        return queryset.filter(
            project_identifier__in=_identifiers_for_project_uuid(value)
        )

    class Meta:
        model = models.CachedProjectStorageReport
        fields = []


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

    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid"
    )
    project_template_uuid = core_filters.RelatedUUIDFilter(
        view_name="openportal-project-template-detail",
        field_name="project_template__uuid",
    )
    state = core_filters.ReviewStateFilter()
    query = django_filters.CharFilter(method="filter_search")

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(identifier__icontains=value)
            | Q(project__name__icontains=value)
            | Q(project_template__name__icontains=value)
            | Q(details__name__icontains=value)
        )

    class Meta:
        model = models.ManagedProject
        fields = []


class ProjectAccountingSummaryFilter(django_filters.FilterSet):
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    is_active = django_filters.BooleanFilter(method="filter_is_active")

    def filter_is_active(self, queryset, name, value):
        from django.utils import timezone

        today = timezone.now().date()
        if value:
            # Active: no end_date set, or end_date is in the future
            return queryset.filter(end_date__isnull=True) | queryset.filter(
                end_date__gt=today
            )
        else:
            # Inactive: end_date is set and has passed
            return queryset.filter(end_date__lte=today)

    class Meta:
        model = structure_models.Project
        fields = []
