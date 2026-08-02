import django_filters
from django.db.models import Q
from django_filters.widgets import BooleanWidget

from waldur_core.core import filters as core_filters
from waldur_core.core.enums import CoreStates
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.models import Offering
from waldur_openstack.utils import get_valid_availability_zones

from . import models


class TenantFilterSet(django_filters.FilterSet):
    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail",
        field_name="tenant__uuid",
        label="Tenant UUID",
    )
    tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail",
        field_name="tenant__uuid",
        label="Tenant URL",
    )


class SharedTenantFilterSet(django_filters.FilterSet):
    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail", method="filter_tenant", label="Tenant UUID"
    )
    tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail",
        method="filter_tenant",
        label="Tenant URL",
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        method="filter_offering",
        label="Offering UUID",
    )

    def filter_tenant(self, queryset, name, value):
        try:
            tenant = models.Tenant.objects.get(uuid=value)
        except models.Tenant.DoesNotExist:
            return queryset.none()
        return queryset.filter(tenants=tenant)

    def filter_offering(self, queryset, name, value):
        """Narrow service properties to those an offering can use.

        An offering's scope is a generic relation, and OpenStack uses two kinds.
        The tenant-provisioning offering is scoped to the service settings,
        while the per-tenant instance and volume offerings that Waldur creates
        alongside a tenant are scoped to that tenant. Assuming settings meant a
        tenant-scoped offering reached a Tenant queryset with a Tenant instance,
        which Django rejects outright — the request failed with a 500 rather
        than an empty or filtered result.
        """
        try:
            offering = Offering.objects.get(uuid=value)
        except Offering.DoesNotExist:
            return queryset.none()

        scope = offering.scope
        if isinstance(scope, models.Tenant):
            return queryset.filter(tenants=scope).distinct()
        if isinstance(scope, structure_models.ServiceSettings):
            tenants = models.Tenant.objects.filter(service_settings=scope)
            if tenants.exists():
                return queryset.filter(tenants__in=tenants).distinct()
            # Fall back to service settings level when no tenants exist yet
            return queryset.filter(settings=scope)
        return queryset.none()


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
        field_name="port", lookup_expr="isnull", widget=BooleanWidget, label="Is free"
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
    o = django_filters.OrderingFilter(fields=("cores", "ram", "disk"), label="Ordering")
    name_iregex = django_filters.CharFilter(
        field_name="name", lookup_expr="iregex", label="Name (regex)"
    )

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
    show_duplicate_names = django_filters.BooleanFilter(
        method="filter_show_duplicate_names",
        label="Show duplicate image names",
        widget=BooleanWidget,
    )
    is_rescue_image = django_filters.BooleanFilter(
        method="filter_is_rescue_image",
        label="Filter to images usable as Nova rescue images.",
        widget=BooleanWidget,
    )

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Image

    def filter_show_duplicate_names(self, queryset, name, value):
        if value:
            return queryset.model.all_objects.all()  # type: ignore[attr-defined]
        return queryset

    def filter_is_rescue_image(self, queryset, name, value):
        if value is None:
            return queryset
        # Either property is sufficient to mark an image as a rescue image.
        rescue_q = ~Q(hw_rescue_device="") | ~Q(hw_rescue_bus="")
        return queryset.filter(rescue_q) if value else queryset.exclude(rescue_q)


class VolumeTypeFilter(
    SharedTenantFilterSet, structure_filters.ServicePropertySettingsFilter
):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.VolumeType


class ExternalNetworkFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.ExternalNetwork


class HypervisorFilter(structure_filters.ServicePropertySettingsFilter):
    trait = django_filters.CharFilter(
        method="filter_traits_and",
        label="Trait names with AND logic (comma-separated)",
    )

    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Hypervisor
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            "hypervisor_type",
            "state",
            "status",
        )

    def filter_traits_and(self, queryset, name, value):
        """Filter hypervisors that have ALL specified traits (AND logic).

        Accepts comma-separated trait names (case-insensitive exact match);
        a single value degenerates to a plain single-trait filter. Note that
        CUSTOM_* trait names are only meaningful within a single source, so
        callers should scope by settings_uuid when filtering on them.
        """
        if not value:
            return queryset
        names = [n.strip() for n in value.split(",") if n.strip()]
        for trait_name in names:
            queryset = queryset.filter(traits__name__iexact=trait_name)
        return queryset.distinct()


class HypervisorInventoryFilter(django_filters.FilterSet):
    hypervisor_uuid = core_filters.RelatedUUIDFilter(
        field_name="hypervisor__uuid",
        view_name="openstack-hypervisor-detail",
    )
    settings_uuid = core_filters.RelatedUUIDFilter(
        field_name="hypervisor__settings__uuid",
        view_name="servicesettings-detail",
    )
    resource_class = django_filters.CharFilter(lookup_expr="iexact")

    class Meta:
        model = models.HypervisorInventory
        fields = ("hypervisor_uuid", "settings_uuid", "resource_class")


class RouterFilter(TenantFilterSet, structure_filters.NameFilterSet):
    state = core_filters.MappedMultipleChoiceFilter(CoreStates.choices, label="State")

    class Meta:
        model = models.Router
        fields = ("state",)


class LoadBalancerFilter(TenantFilterSet, structure_filters.NameFilterSet):
    state = core_filters.MappedMultipleChoiceFilter(CoreStates.choices, label="State")

    class Meta:
        model = models.LoadBalancer
        fields = ("state",)


class PoolFilter(structure_filters.NameFilterSet):
    load_balancer_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-loadbalancer-detail",
        field_name="load_balancer__uuid",
        label="Load balancer UUID",
    )
    load_balancer = core_filters.URLFilter(
        view_name="openstack-loadbalancer-detail",
        field_name="load_balancer__uuid",
        label="Load balancer URL",
    )
    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail",
        field_name="load_balancer__tenant__uuid",
        label="Tenant UUID",
    )
    state = core_filters.MappedMultipleChoiceFilter(CoreStates.choices, label="State")

    class Meta:
        model = models.Pool
        fields = ("state",)


class ListenerFilter(structure_filters.NameFilterSet):
    load_balancer_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-loadbalancer-detail",
        field_name="load_balancer__uuid",
        label="Load balancer UUID",
    )
    load_balancer = core_filters.URLFilter(
        view_name="openstack-loadbalancer-detail",
        field_name="load_balancer__uuid",
        label="Load balancer URL",
    )
    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail",
        field_name="load_balancer__tenant__uuid",
        label="Tenant UUID",
    )
    state = core_filters.MappedMultipleChoiceFilter(CoreStates.choices, label="State")

    class Meta:
        model = models.Listener
        fields = ("state",)


class PoolMemberFilter(structure_filters.NameFilterSet):
    pool_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-pool-detail",
        field_name="pool__uuid",
        label="Pool UUID",
    )
    pool = core_filters.URLFilter(
        view_name="openstack-pool-detail",
        field_name="pool__uuid",
        label="Pool URL",
    )
    load_balancer_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-loadbalancer-detail",
        field_name="pool__load_balancer__uuid",
        label="Load balancer UUID",
    )
    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail",
        field_name="pool__load_balancer__tenant__uuid",
        label="Tenant UUID",
    )
    state = core_filters.MappedMultipleChoiceFilter(CoreStates.choices, label="State")

    class Meta:
        model = models.PoolMember
        fields = ("state",)


class HealthMonitorFilter(structure_filters.NameFilterSet):
    pool_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-pool-detail",
        field_name="pool__uuid",
        label="Pool UUID",
    )
    pool = core_filters.URLFilter(
        view_name="openstack-pool-detail",
        field_name="pool__uuid",
        label="Pool URL",
    )
    load_balancer_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-loadbalancer-detail",
        field_name="pool__load_balancer__uuid",
        label="Load balancer UUID",
    )
    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail",
        field_name="pool__load_balancer__tenant__uuid",
        label="Tenant UUID",
    )
    state = core_filters.MappedMultipleChoiceFilter(CoreStates.choices, label="State")

    class Meta:
        model = models.HealthMonitor
        fields = ("state",)


class PortFilter(TenantFilterSet, structure_filters.NameFilterSet):
    o = django_filters.OrderingFilter(
        fields=(
            ("name", "name"),
            ("created", "created"),
            ("mac_address", "mac_address"),
            ("device_owner", "device_owner"),
            ("admin_state_up", "admin_state_up"),
            ("status", "status"),
            ("network__name", "network_name"),
            ("subnet__name", "subnet_name"),
            ("instance__name", "instance_name"),
        ),
        label="Ordering",
    )
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
    network_name = django_filters.CharFilter(
        label="Search by network name", field_name="network__name"
    )
    network_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-network-detail",
        label="Search by network UUID",
        field_name="network__uuid",
    )
    fixed_ips = django_filters.CharFilter(
        label="Search by fixed IP", lookup_expr="icontains"
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
    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail", method="filter_tenant", label="Tenant UUID"
    )
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
    network_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-network-detail",
        field_name="network__uuid",
        label="Network UUID",
    )
    network = core_filters.URLFilter(
        view_name="openstack-network-detail",
        field_name="network__uuid",
        label="Network URL",
    )

    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail", method="filter_tenant", label="Tenant UUID"
    )
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
        view_name="openstack-instance-detail",
        field_name="instance__uuid",
        label="Instance URL",
    )
    instance_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-instance-detail",
        field_name="instance__uuid",
        label="Instance UUID",
    )

    snapshot = core_filters.URLFilter(
        view_name="openstack-snapshot-detail",
        field_name="restoration__snapshot__uuid",
        label="Snapshot URL",
    )
    snapshot_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-snapshot-detail",
        field_name="restoration__snapshot__uuid",
        label="Snapshot UUID",
    )

    availability_zone_name = django_filters.CharFilter(
        field_name="availability_zone__name", label="Availability zone name"
    )

    attach_instance_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-instance-detail",
        method="filter_attach_instance",
        label="Filter for attachment to instance UUID",
    )

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
    source_volume_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-volume-detail",
        field_name="source_volume__uuid",
        label="Source volume UUID",
    )
    source_volume = core_filters.URLFilter(
        view_name="openstack-volume-detail",
        field_name="source_volume__uuid",
        label="Source volume URL",
    )
    backup_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-backup-detail",
        field_name="backups__uuid",
        label="Backup UUID",
    )
    backup = core_filters.URLFilter(
        view_name="openstack-backup-detail",
        field_name="backups__uuid",
        label="Backup URL",
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
    external_ip = django_filters.CharFilter(
        field_name="ports__floating_ips__address", label="External IP"
    )
    availability_zone_name = django_filters.CharFilter(
        field_name="availability_zone__name", label="Availability zone name"
    )
    attach_volume_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-volume-detail",
        method="filter_attach_volume",
        label="Filter for attachment to volume UUID",
    )
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
        view_name="openstack-instance-detail",
        field_name="instance__uuid",
        label="Instance URL",
    )
    instance_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-instance-detail",
        field_name="instance__uuid",
        label="Instance UUID",
    )

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Backup


class VolumeAvailabilityZoneFilter(
    TenantFilterSet, structure_filters.ServicePropertySettingsFilter
):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.VolumeAvailabilityZone


class NetworkRBACPolicyFilter(django_filters.FilterSet):
    tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail",
        field_name="network__tenant__uuid",
        label="Tenant UUID",
    )
    tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail",
        field_name="network__tenant__uuid",
        label="Tenant URL",
    )

    network_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-network-detail",
        field_name="network__uuid",
        label="Network UUID",
    )
    network = core_filters.URLFilter(
        view_name="openstack-network-detail",
        field_name="network__uuid",
        label="Network URL",
    )

    target_tenant_uuid = core_filters.RelatedUUIDFilter(
        view_name="openstack-tenant-detail",
        field_name="target_tenant__uuid",
        label="Target tenant UUID",
    )
    target_tenant = core_filters.URLFilter(
        view_name="openstack-tenant-detail",
        field_name="target_tenant__uuid",
        label="Target tenant URL",
    )

    DIRECTION_CHOICES = (
        ("outbound", "Outbound"),
        ("inbound", "Inbound"),
        ("all", "All"),
    )
    direction = django_filters.ChoiceFilter(
        choices=DIRECTION_CHOICES,
        method="filter_direction",
        label="Direction relative to the requesting user",
    )

    def filter_direction(self, queryset, name, value):
        request = self.request
        user = getattr(request, "user", None) if request else None
        if value == "all" or user is None or not user.is_authenticated:
            return queryset
        if user.is_staff or user.is_support:
            return queryset
        from waldur_core.structure.managers import (
            get_connected_customers,
            get_connected_projects,
        )

        connected_projects = get_connected_projects(user)
        connected_customers = get_connected_customers(user)
        if value == "outbound":
            return queryset.filter(
                Q(network__tenant__project__in=connected_projects)
                | Q(network__tenant__project__customer__in=connected_customers)
            )
        if value == "inbound":
            return queryset.filter(
                Q(target_tenant__project__in=connected_projects)
                | Q(target_tenant__project__customer__in=connected_customers)
            )
        return queryset

    class Meta:
        model = models.NetworkRBACPolicy
        fields = ["policy_type"]
