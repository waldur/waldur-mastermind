import django_filters
from django.db.models import Count
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class ImageFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Image


class LocationFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Location

    has_sizes = django_filters.BooleanFilter(
        widget=BooleanWidget, method='filter_has_sizes'
    )

    def filter_has_sizes(self, queryset, name, value):
        if value:
            return queryset.annotate(
                size_count=Count('sizeavailabilityzone__zone')
            ).filter(size_count__gt=0)
        else:
            return queryset.filter(resolution_sla__gte=0)


class SizeFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Size

    location = core_filters.URLFilter(
        view_name='azure-location-detail',
        field_name='sizeavailabilityzone__location__uuid',
        distinct=True,
    )
    location_uuid = django_filters.UUIDFilter(
        field_name='sizeavailabilityzone__location__uuid', distinct=True,
    )
    zone = django_filters.NumberFilter(
        field_name='sizeavailabilityzone__zone', distinct=True
    )


class BaseResourceGroupFilter(structure_filters.BaseResourceFilter):
    resource_group = core_filters.URLFilter(
        view_name='azure-resource-group-detail', field_name='resource_group__uuid'
    )
    resource_group_uuid = django_filters.UUIDFilter(field_name='resource_group__uuid')


class VirtualMachineFilter(BaseResourceGroupFilter):
    class Meta(BaseResourceGroupFilter.Meta):
        model = models.VirtualMachine


class PublicIPFilter(BaseResourceGroupFilter):
    class Meta(BaseResourceGroupFilter.Meta):
        model = models.PublicIP


class SQLServerFilter(BaseResourceGroupFilter):
    class Meta(BaseResourceGroupFilter.Meta):
        model = models.SQLServer


class SQLDatabaseFilter(BaseResourceGroupFilter):
    class Meta(BaseResourceGroupFilter.Meta):
        model = models.SQLDatabase

    server = core_filters.URLFilter(
        view_name='azure-server-detail', field_name='server__uuid'
    )
    server_uuid = django_filters.UUIDFilter(field_name='server__uuid')
