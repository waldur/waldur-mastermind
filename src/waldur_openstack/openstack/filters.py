import django_filters
from django.db.models import Q
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters
from waldur_openstack.openstack.utils import filter_property_for_tenant

from . import models


class TenantFilterSet(django_filters.FilterSet):
    tenant_uuid = django_filters.UUIDFilter(field_name="tenant__uuid")
    tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail", field_name="tenant__uuid"
    )


class SharedTenantFilterSet(django_filters.FilterSet):
    tenant_uuid = django_filters.UUIDFilter(method="filter_tenant")
    tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail", method="filter_tenant"
    )

    def filter_tenant(self, queryset, name, value):
        try:
            tenant = models.Tenant.objects.get(uuid=value)
        except models.Tenant.DoesNotExist:
            return queryset.none()
        return filter_property_for_tenant(queryset, tenant)


class SecurityGroupFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    query = django_filters.CharFilter(method="filter_query")

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.SecurityGroup

    def filter_query(self, queryset, name, value):
        query = queryset.filter(
            Q(name__icontains=value) | Q(description__icontains=value)
        )
        return query


class ServerGroupFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.ServerGroup


class FloatingIPFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    free = django_filters.BooleanFilter(
        field_name="port", lookup_expr="isnull", widget=BooleanWidget
    )

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.FloatingIP
        fields = structure_filters.BaseResourceFilter.Meta.fields + (
            "runtime_state",
            "address",
        )


class FlavorFilter(
    SharedTenantFilterSet, structure_filters.ServicePropertySettingsFilter
):
    o = django_filters.OrderingFilter(fields=("cores", "ram", "disk"))
    name_iregex = django_filters.CharFilter(field_name="name", lookup_expr="iregex")

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Flavor
        fields = dict(
            {
                "cores": ["exact", "gte", "lte"],
                "ram": ["exact", "gte", "lte"],
                "disk": ["exact", "gte", "lte"],
            },
            **{
                field: ["exact"]
                for field in structure_filters.ServicePropertySettingsFilter.Meta.fields
            },
        )


class ImageFilter(
    SharedTenantFilterSet, structure_filters.ServicePropertySettingsFilter
):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Image


class VolumeTypeFilter(
    SharedTenantFilterSet, structure_filters.ServicePropertySettingsFilter
):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.VolumeType


class RouterFilter(TenantFilterSet, structure_filters.NameFilterSet):
    class Meta:
        model = models.Router
        fields = ()


class PortFilter(TenantFilterSet, structure_filters.NameFilterSet):
    o = django_filters.OrderingFilter(fields=(("network__name", "network_name"),))

    class Meta:
        model = models.Port
        fields = ()


class NetworkFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Network
        fields = structure_filters.BaseResourceFilter.Meta.fields + (
            "type",
            "is_external",
        )


class SubNetFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    network_uuid = django_filters.UUIDFilter(field_name="network__uuid")
    network = core_filters.URLFilter(
        view_name="openstack-network-detail", field_name="network__uuid"
    )

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.SubNet
        fields = structure_filters.BaseResourceFilter.Meta.fields + (
            "ip_version",
            "enable_dhcp",
        )
