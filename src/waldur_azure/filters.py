import django_filters

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class ImageFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Image


class LocationFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Location


class SizeFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Size


class BaseResourceGroupFilter(structure_filters.BaseResourceFilter):
    resource_group = core_filters.URLFilter(
        view_name='azure-resource-group-detail',
        name='resource_group__uuid'
    )
    resource_group_uuid = django_filters.UUIDFilter(name='resource_group__uuid')


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
        view_name='azure-server-detail',
        name='server__uuid'
    )
    server_uuid = django_filters.UUIDFilter(name='server__uuid')
