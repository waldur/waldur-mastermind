import django_filters
from django.db.models import Q
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters
from waldur_mastermind.marketplace.models import Offering
from waldur_openstack.utils import get_valid_availability_zones

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
    offering_uuid = django_filters.UUIDFilter(method="filter_offering")

    def filter_tenant(self, queryset, name, value):
        try:
            tenant = models.Tenant.objects.get(uuid=value)
        except models.Tenant.DoesNotExist:
            return queryset.none()
        return queryset.filter(tenants=tenant)

    def filter_offering(self, queryset, name, value):
        try:
            offering = Offering.objects.get(uuid=value)
        except Offering.DoesNotExist:
            return queryset.none()
        if not offering.scope:
            return queryset.none()

        tenants = models.Tenant.objects.filter(service_settings=offering.scope)
        return queryset.filter(tenants__in=tenants).distinct()


class SecurityGroupFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    query = django_filters.CharFilter(
        method="filter_query", label="Search by name or description"
    )

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
    query = django_filters.CharFilter(
        method="filter_query", label="Search by name, MAC address or backend ID"
    )
    has_device_owner = django_filters.BooleanFilter(
        method="filter_has_device_owner", label="Has device owner"
    )
    exclude_subnet_uuids = django_filters.CharFilter(
        method="filter_exclude_subnet_uuids",
        label="Exclude Subnet UUIDs (comma-separated)",
    )

    def filter_has_device_owner(self, queryset, name, value):
        if value:
            return queryset.exclude(device_owner="").exclude(device_owner__isnull=True)
        else:
            return queryset.filter(Q(device_owner="") | Q(device_owner__isnull=True))

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value)
            | Q(mac_address__icontains=value)
            | Q(backend_id__icontains=value)
        )

    def filter_exclude_subnet_uuids(self, queryset, name, value):
        if not value:
            return queryset
        uuids = [uuid.strip() for uuid in value.split(",") if uuid.strip()]
        if uuids:
            return queryset.exclude(subnet__uuid__in=uuids)
        return queryset

    class Meta:
        model = models.Port
        fields = (
            "status",
            "mac_address",
            "backend_id",
            "admin_state_up",
            "device_owner",
            "device_id",
        )


def filter_tenant_fabric(model):
    target_tenant_path = ""
    tenant_path = ""

    if model == models.Network:
        target_tenant_path = "rbac_policies__target_tenant"
        tenant_path = "tenant"
    elif model == models.SubNet:
        target_tenant_path = "network__rbac_policies__target_tenant"
        tenant_path = "network__tenant"

    def filter_tenant(self, queryset, name, value):
        if name == "tenant":
            uuid = list(filter(None, value.split("/")))[-1]
        else:
            uuid = value

        try:
            tenant = models.Tenant.objects.get(uuid=uuid)
        except models.Tenant.DoesNotExist:
            return queryset.none()

        direct_networks = queryset.filter(**{tenant_path: tenant})
        rbac_networks = queryset.filter(**{target_tenant_path: tenant})

        if self.request.query_params.get("direct_only") == "true":
            return direct_networks

        if self.request.query_params.get("rbac_only") == "true":
            return rbac_networks

        return (direct_networks | rbac_networks).distinct()

    return filter_tenant


class NetworkFilter(structure_filters.BaseResourceFilter):
    tenant_uuid = django_filters.UUIDFilter(method="filter_tenant", label="Tenant UUID")
    tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail", method="filter_tenant", label="Tenant URL"
    )
    direct_only = django_filters.BooleanFilter(
        method="filter_direct_only", label="Direct only"
    )
    rbac_only = django_filters.BooleanFilter(
        method="filter_rbac_only", label="RBAC only"
    )

    filter_tenant = filter_tenant_fabric(models.Network)

    def filter_direct_only(self, queryset, name, value):
        return queryset

    def filter_rbac_only(self, queryset, name, value):
        return queryset

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Network
        fields = structure_filters.BaseResourceFilter.Meta.fields + (
            "type",
            "is_external",
        )


class SubNetFilter(structure_filters.BaseResourceFilter):
    network_uuid = django_filters.UUIDFilter(field_name="network__uuid")
    network = core_filters.URLFilter(
        view_name="openstack-network-detail", field_name="network__uuid"
    )

    tenant_uuid = django_filters.UUIDFilter(method="filter_tenant", label="Tenant UUID")
    tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail", method="filter_tenant", label="Tenant URL"
    )
    direct_only = django_filters.BooleanFilter(
        method="filter_direct_only", label="Direct only"
    )
    rbac_only = django_filters.BooleanFilter(
        method="filter_rbac_only", label="RBAC only"
    )

    filter_tenant = filter_tenant_fabric(models.SubNet)

    def filter_direct_only(self, queryset, name, value):
        return queryset

    def filter_rbac_only(self, queryset, name, value):
        return queryset

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.SubNet
        fields = structure_filters.BaseResourceFilter.Meta.fields + (
            "ip_version",
            "enable_dhcp",
        )


class VolumeFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    instance = core_filters.URLFilter(
        view_name="openstack-instance-detail", field_name="instance__uuid"
    )
    instance_uuid = django_filters.UUIDFilter(field_name="instance__uuid")

    snapshot = core_filters.URLFilter(
        view_name="openstack-snapshot-detail",
        field_name="restoration__snapshot__uuid",
    )
    snapshot_uuid = django_filters.UUIDFilter(field_name="restoration__snapshot__uuid")

    availability_zone_name = django_filters.CharFilter(
        field_name="availability_zone__name"
    )

    attach_instance_uuid = django_filters.UUIDFilter(method="filter_attach_instance")

    def filter_attach_instance(self, queryset, name, value):
        """
        This filter is used by volume attachment dialog for instance.
        It allows to filter out volumes that could be attached to the given instance.
        """
        try:
            instance = models.Instance.objects.get(uuid=value)
        except models.Volume.DoesNotExist:
            return queryset.none()

        queryset = queryset.filter(
            tenant=instance.tenant, project=instance.project
        ).exclude(instance=instance)

        zones_map = get_valid_availability_zones(instance)
        if instance.availability_zone and zones_map:
            zone_names = {
                nova_zone
                for (nova_zone, cinder_zone) in zones_map.items()
                if cinder_zone == instance.availability_zone.name
            }
            nova_zones = models.InstanceAvailabilityZone.objects.filter(
                tenant=instance.tenant, name__in=zone_names, available=True
            )
            queryset = queryset.filter(availability_zone__in=nova_zones)
        return queryset

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Volume
        fields = structure_filters.BaseResourceFilter.Meta.fields + ("runtime_state",)

    ORDERING_FIELDS = structure_filters.BaseResourceFilter.ORDERING_FIELDS + (
        ("instance__name", "instance_name"),
        ("size", "size"),
    )


class SnapshotFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    source_volume_uuid = django_filters.UUIDFilter(field_name="source_volume__uuid")
    source_volume = core_filters.URLFilter(
        view_name="openstack-volume-detail", field_name="source_volume__uuid"
    )
    backup_uuid = django_filters.UUIDFilter(field_name="backups__uuid")
    backup = core_filters.URLFilter(
        view_name="openstack-backup-detail", field_name="backups__uuid"
    )

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Snapshot
        fields = structure_filters.BaseResourceFilter.Meta.fields + ("runtime_state",)

    ORDERING_FIELDS = structure_filters.BaseResourceFilter.ORDERING_FIELDS + (
        ("source_volume__name", "source_volume_name"),
        ("size", "size"),
    )


class InstanceAvailabilityZoneFilter(
    TenantFilterSet, structure_filters.ServicePropertySettingsFilter
):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.InstanceAvailabilityZone


class InstanceFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    external_ip = django_filters.CharFilter(field_name="ports__floating_ips__address")
    availability_zone_name = django_filters.CharFilter(
        field_name="availability_zone__name"
    )
    attach_volume_uuid = django_filters.UUIDFilter(method="filter_attach_volume")
    query = django_filters.CharFilter(
        method="filter_query", label="Search by name, internal IP, or external IP"
    )

    def filter_attach_volume(self, queryset, name, value):
        """
        This filter is used by volume attachment dialog.
        It allows to filter out instances that could be attached to the given volume.
        """
        try:
            volume = models.Volume.objects.get(uuid=value)
        except models.Volume.DoesNotExist:
            return queryset.none()

        queryset = queryset.filter(tenant=volume.tenant, project=volume.project)

        zones_map = get_valid_availability_zones(volume)
        if volume.availability_zone and zones_map:
            zone_names = {
                nova_zone
                for (nova_zone, cinder_zone) in zones_map.items()
                if cinder_zone == volume.availability_zone.name
            }
            nova_zones = models.InstanceAvailabilityZone.objects.filter(
                tenant=volume.tenant,
                name__in=zone_names,
                available=True,
            )
            queryset = queryset.filter(availability_zone__in=nova_zones)
        return queryset

    def filter_query(self, queryset, name, value):
        """
        This filter allows searching instances by name, internal IP, or external IP.
        """
        return queryset.filter(
            Q(name__icontains=value)
            | Q(ports__floating_ips__address__icontains=value)
            | Q(ports__fixed_ips__icontains=value)
        ).distinct()

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Instance
        fields = structure_filters.BaseResourceFilter.Meta.fields + (
            "runtime_state",
            "external_ip",
        )

    ORDERING_FIELDS = structure_filters.BaseResourceFilter.ORDERING_FIELDS + (
        ("ports__fixed_ips__0__ip_address", "ip_address"),
        ("ports__floating_ips__address", "external_ips"),
    )


class BackupFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    instance = core_filters.URLFilter(
        view_name="openstack-instance-detail", field_name="instance__uuid"
    )
    instance_uuid = django_filters.UUIDFilter(field_name="instance__uuid")

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Backup


class VolumeAvailabilityZoneFilter(
    TenantFilterSet, structure_filters.ServicePropertySettingsFilter
):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.VolumeAvailabilityZone


class NetworkRBACPolicyFilter(django_filters.FilterSet):
    network_uuid = django_filters.UUIDFilter(field_name="network__uuid")
    network = core_filters.URLFilter(
        view_name="openstack-network-detail", field_name="network__uuid"
    )

    target_tenant_uuid = django_filters.UUIDFilter(field_name="target_tenant__uuid")
    target_tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail", field_name="target_tenant__uuid"
    )

    class Meta:
        model = models.NetworkRBACPolicy
        fields = ["policy_type"]
