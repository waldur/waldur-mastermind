import django_filters

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class ServiceProjectLinkFilter(structure_filters.BaseServiceProjectLinkFilter):
    service = core_filters.URLFilter(view_name='vmware-detail', field_name='service__uuid')

    class Meta(structure_filters.BaseServiceProjectLinkFilter.Meta):
        model = models.VMwareServiceProjectLink


class VirtualMachineFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.VirtualMachine
        fields = structure_filters.BaseResourceFilter.Meta.fields + ('runtime_state',)


class PortFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Port

    vm = core_filters.URLFilter(view_name='vmware-virtual-machine-detail', field_name='vm__uuid')
    vm_uuid = django_filters.UUIDFilter(field_name='vm__uuid')

    network = core_filters.URLFilter(view_name='vmware-network-detail', field_name='network__uuid')
    network_uuid = django_filters.UUIDFilter(field_name='network__uuid')


class DiskFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Disk

    vm = core_filters.URLFilter(view_name='vmware-virtual-machine-detail', field_name='vm__uuid')
    vm_uuid = django_filters.UUIDFilter(field_name='vm__uuid')
    ORDERING_FIELDS = structure_filters.BaseResourceFilter.ORDERING_FIELDS + (
        ('size', 'size'),
    )


class TemplateFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Template


class ClusterFilter(structure_filters.ServicePropertySettingsFilter):
    customer_uuid = django_filters.UUIDFilter(method='filter_customer', label='Customer UUID')

    def filter_customer(self, queryset, name, value):
        return queryset.filter(customercluster__customer__uuid=value)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Cluster


class NetworkFilter(structure_filters.ServicePropertySettingsFilter):
    customer_uuid = django_filters.UUIDFilter(method='filter_customer', label='Customer UUID')
    customer_pair_uuid = django_filters.UUIDFilter(method='filter_customer_pair', label='Customer UUID')

    def filter_customer(self, queryset, name, value):
        return queryset.filter(customernetwork__customer__uuid=value)

    def filter_customer_pair(self, queryset, name, value):
        return queryset.filter(customernetworkpair__customer__uuid=value)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Network


class DatastoreFilter(structure_filters.ServicePropertySettingsFilter):
    customer_uuid = django_filters.UUIDFilter(method='filter_customer', label='Customer UUID')

    def filter_customer(self, queryset, name, value):
        return queryset.filter(customerdatastore__customer__uuid=value)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Datastore


class FolderFilter(structure_filters.ServicePropertySettingsFilter):
    customer_uuid = django_filters.UUIDFilter(method='filter_customer', label='Customer UUID')

    def filter_customer(self, queryset, name, value):
        return queryset.filter(customerfolder__customer__uuid=value)

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Folder
