import django_filters
from django.db.models import Q
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters
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

    def filter_tenant(self, queryset, name, value):
        try:
            tenant = models.Tenant.objects.get(uuid=value)
        except models.Tenant.DoesNotExist:
            return queryset.none()
        return queryset.filter(tenants=tenant)


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

    snapshot_schedule = core_filters.URLFilter(
        view_name="openstack-snapshot-schedule-detail",
        field_name="snapshot_schedule__uuid",
    )
    snapshot_schedule_uuid = django_filters.UUIDFilter(
        field_name="snapshot_schedule__uuid"
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
    backup_schedule = core_filters.URLFilter(
        view_name="openstack-backup-schedule-detail",
        field_name="backup_schedule__uuid",
    )
    backup_schedule_uuid = django_filters.UUIDFilter(field_name="backup_schedule__uuid")

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Backup


class BackupScheduleFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    instance = core_filters.URLFilter(
        view_name="openstack-instance-detail", field_name="instance__uuid"
    )
    instance_uuid = django_filters.UUIDFilter(field_name="instance__uuid")

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.BackupSchedule


class SnapshotScheduleFilter(TenantFilterSet, structure_filters.BaseResourceFilter):
    source_volume = core_filters.URLFilter(
        view_name="openstack-volume-detail", field_name="source_volume__uuid"
    )
    source_volume_uuid = django_filters.UUIDFilter(field_name="source_volume__uuid")

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.SnapshotSchedule


class VolumeAvailabilityZoneFilter(
    TenantFilterSet, structure_filters.ServicePropertySettingsFilter
):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.VolumeAvailabilityZone
