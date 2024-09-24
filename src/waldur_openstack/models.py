import logging
from urllib.parse import urlparse

from django.core import validators
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.template.defaultfilters import slugify
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.fields import JSONField
from waldur_core.core.mixins import ActionMixin
from waldur_core.quotas import models as quotas_models
from waldur_core.quotas.fields import QuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure import utils as structure_utils
from waldur_core.structure.managers import filter_queryset_for_user
from waldur_geo_ip.utils import get_coordinates_by_ip

logger = logging.getLogger(__name__)


def build_tenants_query(user):
    tenants = filter_queryset_for_user(Tenant.objects.all(), user)
    settings = filter_queryset_for_user(
        structure_models.ServiceSettings.objects.all(), user
    )
    return Q(Q(tenants=None) | Q(tenants__in=tenants)) & Q(settings__in=settings)


class Tenant(structure_models.PrivateCloud):
    class Quotas(QuotaModelMixin.Quotas):
        vcpu = QuotaField(default_limit=20, is_backend=True)
        ram = QuotaField(default_limit=51200, is_backend=True)
        storage = QuotaField(default_limit=1024000, is_backend=True)
        instances = QuotaField(default_limit=30, is_backend=True)
        security_group_count = QuotaField(default_limit=100, is_backend=True)
        security_group_rule_count = QuotaField(default_limit=100, is_backend=True)
        floating_ip_count = QuotaField(default_limit=50, is_backend=True)
        port_count = QuotaField(is_backend=True)
        volumes = QuotaField(default_limit=50, is_backend=True)
        volumes_size = QuotaField(is_backend=True)
        snapshots = QuotaField(default_limit=50, is_backend=True)
        snapshots_size = QuotaField(is_backend=True)
        network_count = QuotaField(default_limit=10, is_backend=True)
        subnet_count = QuotaField(default_limit=10, is_backend=True)

    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, blank=True, null=True)

    internal_network_id = models.CharField(max_length=64, blank=True)
    external_network_id = models.CharField(max_length=64, blank=True)
    availability_zone = models.CharField(
        max_length=100,
        blank=True,
        help_text=_(
            "Optional availability group. Will be used for all instances provisioned in this tenant"
        ),
    )
    default_volume_type_name = models.CharField(
        max_length=100,
        blank=True,
        help_text=_("Volume type name to use when creating volumes."),
    )
    user_username = models.CharField(max_length=50, blank=True)
    user_password = models.CharField(max_length=50, blank=True)

    tracker = FieldTracker()

    class Meta:
        unique_together = ("service_settings", "backend_id")

    @classmethod
    def generate_username(cls, name):
        """
        Generates random valid tenant user name based on tenant name
        :param name: tenant name
        :return: username
        """
        return slugify(name)[:25] + "-user-%s" % core_utils.pwgen(4)

    def get_backend(self):
        return self.service_settings.get_backend()

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "name",
            "description",
            "error_message",
            "runtime_state",
        )

    def get_access_url(self):
        settings = self.service_settings
        access_url = settings.get_option("access_url")
        if access_url:
            return access_url

        if settings.backend_url:
            parsed = urlparse(settings.backend_url)
            return f"{parsed.scheme}://{parsed.hostname}/dashboard"

    def format_quota(self, name, limit):
        if name == self.Quotas.vcpu.name:
            return int(limit)
        elif name in (self.Quotas.storage.name, self.Quotas.ram.name):
            return _("%s GB") % int(limit / 1024)
        else:
            return limit


class Flavor(structure_models.ServiceProperty):
    cores = models.PositiveSmallIntegerField(help_text=_("Number of cores in a VM"))
    ram = models.PositiveIntegerField(help_text=_("Memory size in MiB"))
    disk = models.PositiveIntegerField(help_text=_("Root disk size in MiB"))
    tenants = models.ManyToManyField(to=Tenant, related_name="flavors")

    class Permissions:
        build_query = build_tenants_query

    @classmethod
    def get_url_name(cls):
        return "openstack-flavor"

    @classmethod
    def get_backend_fields(cls):
        readonly_fields = super().get_backend_fields()
        return readonly_fields + ("cores", "ram", "disk")

    def get_backend(self):
        return self.settings.get_backend()


class Image(structure_models.ServiceProperty):
    min_disk = models.PositiveIntegerField(
        default=0, help_text=_("Minimum disk size in MiB")
    )
    min_ram = models.PositiveIntegerField(
        default=0, help_text=_("Minimum memory size in MiB")
    )
    tenants = models.ManyToManyField(to=Tenant, related_name="images")

    class Permissions:
        build_query = build_tenants_query

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + ("min_disk", "min_ram")

    @classmethod
    def get_url_name(cls):
        return "openstack-image"


class VolumeType(core_models.DescribableMixin, structure_models.ServiceProperty):
    tenants = models.ManyToManyField(to=Tenant, related_name="volume_types")

    class Meta:
        unique_together = ("settings", "backend_id")

    class Permissions:
        build_query = build_tenants_query

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "openstack-volume-type"


class ServerGroup(structure_models.SubResource):
    AFFINITY = "affinity"

    POLICIES = ((AFFINITY, "Affinity"),)

    policy = models.CharField(max_length=40, blank=True, choices=POLICIES)

    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="server_groups"
    )

    tracker = FieldTracker()

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-server-group"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "name",
            "policy",
        )


class SecurityGroup(structure_models.SubResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="security_groups"
    )

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-sgp"

    def increase_backend_quotas_usage(self, validate=False):
        self.tenant.add_quota_usage("security_group_count", 1, validate=validate)
        self.tenant.add_quota_usage(
            "security_group_rule_count",
            self.rules.count(),
            validate=validate,
        )

    def decrease_backend_quotas_usage(self):
        self.tenant.add_quota_usage("security_group_count", -1)
        self.tenant.add_quota_usage("security_group_rule_count", -self.rules.count())

    def change_backend_quotas_usage_on_rules_update(
        self, old_rules_count, validate=False
    ):
        count = self.rules.count() - old_rules_count
        self.tenant.add_quota_usage(
            "security_group_rule_count", count, validate=validate
        )

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + ("name", "description")


class SecurityGroupRule(
    core_models.LoggableMixin, core_models.DescribableMixin, models.Model
):
    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"

    PROTOCOLS = (
        (TCP, "tcp"),
        (UDP, "udp"),
        (ICMP, "icmp"),
    )

    INGRESS = "ingress"
    EGRESS = "egress"

    DIRECTIONS = (
        (INGRESS, "ingress"),
        (EGRESS, "egress"),
    )

    IPv4 = "IPv4"
    IPv6 = "IPv6"

    ETHER_TYPES = (
        (IPv4, "IPv4"),
        (IPv6, "IPv6"),
    )

    # Empty string represents any protocol
    protocol = models.CharField(max_length=40, blank=True, choices=PROTOCOLS)
    from_port = models.IntegerField(
        validators=[validators.MaxValueValidator(65535)], null=True
    )
    to_port = models.IntegerField(
        validators=[validators.MaxValueValidator(65535)], null=True
    )
    cidr = models.CharField(max_length=255, blank=True, null=True)
    direction = models.CharField(max_length=8, default=INGRESS, choices=DIRECTIONS)
    ethertype = models.CharField(max_length=40, default=IPv4, choices=ETHER_TYPES)

    backend_id = models.CharField(max_length=36, blank=True)

    def __str__(self):
        return f"{self.security_group} ({self.protocol}): {self.cidr} ({self.from_port} -> {self.to_port})"

    security_group = models.ForeignKey(
        on_delete=models.CASCADE, to=SecurityGroup, related_name="rules"
    )
    remote_group = models.ForeignKey(
        on_delete=models.CASCADE,
        to=SecurityGroup,
        related_name="+",
        null=True,
        blank=True,
    )
    tracker = FieldTracker()

    def get_log_fields(self):
        return (
            "security_group",
            "protocol",
            "from_port",
            "to_port",
            "cidr",
            "direction",
            "ethertype",
            "backend_id",
        )


class FloatingIP(core_models.RuntimeStateMixin, structure_models.SubResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="floating_ips"
    )
    address = models.GenericIPAddressField(
        null=True, blank=True, protocol="IPv4", default=None
    )
    backend_network_id = models.CharField(max_length=255, editable=False)
    port = models.ForeignKey(
        on_delete=models.SET_NULL,
        to="Port",
        related_name="floating_ips",
        blank=True,
        null=True,
    )

    tracker = FieldTracker()

    class Meta:
        unique_together = ("tenant", "address")
        verbose_name = _("Floating IP")
        verbose_name_plural = _("Floating IPs")

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-fip"

    def __str__(self):
        return f"{self.address}:{self.runtime_state} ({self.service_settings})"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "name",
            "description",
            "address",
            "backend_network_id",
            "runtime_state",
            "port",
        )

    def increase_backend_quotas_usage(self, validate=False):
        self.tenant.add_quota_usage("floating_ip_count", 1, validate=validate)

    def decrease_backend_quotas_usage(self):
        self.tenant.add_quota_usage("floating_ip_count", -1)


class Router(structure_models.SubResource):
    tenant: Tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="routers"
    )
    routes = JSONField(default=list)
    fixed_ips = JSONField(default=list)

    tracker = FieldTracker()

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-router"


class Network(core_models.RuntimeStateMixin, structure_models.SubResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="networks"
    )
    is_external = models.BooleanField(default=False)
    type = models.CharField(max_length=50, blank=True)
    segmentation_id = models.IntegerField(null=True)
    mtu = models.IntegerField(
        null=True,
        help_text=_(
            "The maximum transmission unit (MTU) value to address fragmentation."
        ),
        validators=[
            validators.MinValueValidator(68),
            validators.MaxValueValidator(9000),
        ],
    )

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-network"

    def increase_backend_quotas_usage(self, validate=False):
        self.tenant.add_quota_usage("network_count", 1, validate=validate)

    def decrease_backend_quotas_usage(self):
        self.tenant.add_quota_usage("network_count", -1)

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "name",
            "description",
            "is_external",
            "type",
            "segmentation_id",
            "runtime_state",
            "mtu",
        )


class SubNet(structure_models.SubResource):
    tenant: Tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="+"
    )
    network = models.ForeignKey(
        on_delete=models.CASCADE, to=Network, related_name="subnets"
    )
    disable_gateway = models.BooleanField(default=False)
    host_routes = JSONField(
        default=list,
        help_text=_("List of additional routes for the subnet."),
    )
    cidr = models.CharField(max_length=32, blank=True)
    gateway_ip = models.GenericIPAddressField(protocol="IPv4", null=True)
    allocation_pools = JSONField(default=dict)
    ip_version = models.SmallIntegerField(default=4)
    enable_dhcp = models.BooleanField(default=True)
    dns_nameservers = JSONField(
        default=list,
        help_text=_("List of DNS name servers associated with the subnet."),
    )
    is_connected = models.BooleanField(
        default=True, help_text=_("Is subnet connected to the default tenant router.")
    )

    class Meta:
        verbose_name = _("Subnet")
        verbose_name_plural = _("Subnets")

    def get_backend(self):
        return self.network.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-subnet"

    def increase_backend_quotas_usage(self, validate=False):
        self.network.tenant.add_quota_usage("subnet_count", 1, validate=validate)

    def decrease_backend_quotas_usage(self):
        self.network.tenant.add_quota_usage("subnet_count", -1)

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "name",
            "description",
            "allocation_pools",
            "cidr",
            "ip_version",
            "enable_dhcp",
            "gateway_ip",
            "dns_nameservers",
            "host_routes",
            "is_connected",
        )

    def get_log_fields(self):
        return super().get_log_fields() + ("network",)


class Port(structure_models.SubResource):
    tenant: Tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="ports"
    )
    network = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Network,
        related_name="ports",
        null=True,
        blank=True,
    )
    port_security_enabled = models.BooleanField(default=True)
    security_groups = models.ManyToManyField(SecurityGroup, related_name="ports")
    instance = models.ForeignKey(
        on_delete=models.CASCADE,
        to="Instance",
        related_name="ports",
        null=True,
        blank=True,
    )
    subnet = models.ForeignKey(
        on_delete=models.CASCADE,
        to=SubNet,
        related_name="ports",
        null=True,
        blank=True,
    )
    tracker = FieldTracker()
    # TODO: Use dedicated field: https://github.com/django-macaddress/django-macaddress
    mac_address = models.CharField(max_length=32, blank=True)
    fixed_ips = JSONField(
        default=list,
        help_text=_(
            "A list of tuples (ip_address, subnet_id), where ip_address can be both IPv4 and IPv6 "
            "and subnet_id is a backend id of the subnet"
        ),
    )
    backend_id = models.CharField(max_length=255, blank=True)
    allowed_address_pairs = JSONField(
        default=list,
        help_text=_(
            "A server can send a packet with source address which matches one of the specified allowed address pairs."
        ),
    )
    # Usually device refers to instance or router
    device_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )
    device_owner = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "fixed_ips",
            "mac_address",
            "allowed_address_pairs",
            "device_id",
            "device_owner",
        )

    @classmethod
    def get_url_name(cls):
        return "openstack-port"

    def __str__(self):
        return self.name


class CustomerOpenStack(TimeStampedModel):
    settings = models.ForeignKey(
        structure_models.ServiceSettings,
        on_delete=models.CASCADE,
        limit_choices_to={"shared": True, "type": "OpenStack"},
    )
    customer = models.ForeignKey(on_delete=models.CASCADE, to=structure_models.Customer)
    external_network_id = models.CharField(
        _("OpenStack external network ID"), max_length=255
    )

    class Meta:
        verbose_name = _("Organization OpenStack settings")
        verbose_name_plural = _("Organization OpenStack settings")
        unique_together = ("settings", "customer")


class TenantQuotaMixin(quotas_models.SharedQuotaMixin):
    """
    It allows to update both service settings and shared tenant quotas.
    """

    def get_quota_scopes(self) -> list[quotas_models.QuotaModelMixin]:
        return [self.tenant]


class VolumeAvailabilityZone(structure_models.BaseServiceProperty):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="volume_availability_zones"
    )
    settings = models.ForeignKey(
        on_delete=models.CASCADE, to=structure_models.ServiceSettings, related_name="+"
    )
    available = models.BooleanField(default=True)

    class Meta:
        unique_together = ("settings", "name")

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "openstack-volume-availability-zone"


class Volume(ActionMixin, TenantQuotaMixin, structure_models.Volume):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="volumes"
    )
    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, blank=True, null=True)

    instance = models.ForeignKey(
        on_delete=models.CASCADE,
        to="Instance",
        related_name="volumes",
        blank=True,
        null=True,
    )
    device = models.CharField(
        max_length=50,
        blank=True,
        validators=[
            RegexValidator(
                "^/dev/[a-zA-Z0-9]+$",
                message=_('Device should match pattern "/dev/alphanumeric+"'),
            )
        ],
        help_text=_("Name of volume as instance device e.g. /dev/vdb."),
    )
    bootable = models.BooleanField(default=False)
    metadata = JSONField(blank=True)
    image = models.ForeignKey(Image, blank=True, null=True, on_delete=models.SET_NULL)
    image_name = models.CharField(max_length=150, blank=True)
    image_metadata = JSONField(blank=True)
    type: VolumeType = models.ForeignKey(
        VolumeType, blank=True, null=True, on_delete=models.SET_NULL
    )
    availability_zone = models.ForeignKey(
        VolumeAvailabilityZone, blank=True, null=True, on_delete=models.SET_NULL
    )
    source_snapshot: "Snapshot" = models.ForeignKey(
        "Snapshot",
        related_name="volumes",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
    )
    tracker = FieldTracker()

    class Meta:
        unique_together = ("service_settings", "backend_id")

    def get_quota_deltas(self):
        from waldur_openstack.utils import volume_type_name_to_quota_name

        deltas = {
            "volumes": 1,
            "volumes_size": self.size,
            "storage": self.size,
        }
        if self.type:
            deltas[volume_type_name_to_quota_name(self.type.name)] = self.size / 1024
        return deltas

    @classmethod
    def get_url_name(cls):
        return "openstack-volume"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "name",
            "description",
            "size",
            "metadata",
            "type",
            "bootable",
            "runtime_state",
            "device",
            "instance",
            "availability_zone",
            "image",
            "image_metadata",
            "image_name",
        )


class Snapshot(ActionMixin, TenantQuotaMixin, structure_models.Snapshot):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="snapshots"
    )
    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, blank=True, null=True)

    source_volume: Volume = models.ForeignKey(
        Volume, related_name="snapshots", null=True, on_delete=models.CASCADE
    )
    metadata = JSONField(blank=True)
    snapshot_schedule = models.ForeignKey(
        "SnapshotSchedule",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="snapshots",
    )

    tracker = FieldTracker()

    kept_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Guaranteed time of snapshot retention. If null - keep forever."),
    )

    class Meta:
        unique_together = ("service_settings", "backend_id")

    @classmethod
    def get_url_name(cls):
        return "openstack-snapshot"

    def get_quota_deltas(self):
        deltas = {
            "snapshots": 1,
            "snapshots_size": self.size,
        }
        return deltas

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "name",
            "description",
            "size",
            "metadata",
            "source_volume",
            "runtime_state",
        )


class SnapshotRestoration(core_models.UuidMixin, TimeStampedModel):
    snapshot = models.ForeignKey(
        on_delete=models.CASCADE, to=Snapshot, related_name="restorations"
    )
    volume = models.OneToOneField(
        Volume, related_name="restoration", on_delete=models.CASCADE
    )

    class Permissions:
        customer_path = "snapshot__project__customer"
        project_path = "snapshot__project"


class InstanceAvailabilityZone(structure_models.BaseServiceProperty):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="instance_availability_zones"
    )
    settings = models.ForeignKey(
        on_delete=models.CASCADE, to=structure_models.ServiceSettings, related_name="+"
    )
    available = models.BooleanField(default=True)

    class Meta:
        unique_together = ("settings", "name")

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "openstack-instance-availability-zone"


class Instance(ActionMixin, TenantQuotaMixin, structure_models.VirtualMachine):
    class RuntimeStates:
        # All possible OpenStack Instance states on backend.
        # See https://docs.openstack.org/developer/nova/vmstates.html
        ACTIVE = "ACTIVE"
        BUILDING = "BUILDING"
        DELETED = "DELETED"
        SOFT_DELETED = "SOFT_DELETED"
        ERROR = "ERROR"
        UNKNOWN = "UNKNOWN"
        HARD_REBOOT = "HARD_REBOOT"
        REBOOT = "REBOOT"
        REBUILD = "REBUILD"
        PASSWORD = "PASSWORD"
        PAUSED = "PAUSED"
        RESCUED = "RESCUED"
        RESIZED = "RESIZED"
        REVERT_RESIZE = "REVERT_RESIZE"
        SHUTOFF = "SHUTOFF"
        STOPPED = "STOPPED"
        SUSPENDED = "SUSPENDED"
        VERIFY_RESIZE = "VERIFY_RESIZE"

    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="instances"
    )
    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, blank=True, null=True)

    availability_zone = models.ForeignKey(
        InstanceAvailabilityZone, blank=True, null=True, on_delete=models.SET_NULL
    )
    flavor_name = models.CharField(max_length=255, blank=True)
    flavor_disk = models.PositiveIntegerField(
        default=0, help_text=_("Flavor disk size in MiB")
    )
    security_groups = models.ManyToManyField(
        SecurityGroup, related_name="instances", blank=True
    )
    server_group = models.ForeignKey(
        ServerGroup, blank=True, null=True, on_delete=models.SET_NULL
    )
    subnets = models.ManyToManyField(SubNet, through=Port)
    hypervisor_hostname = models.CharField(max_length=255, blank=True)

    connect_directly_to_external_network = models.BooleanField(default=False)
    directly_connected_ips = models.CharField(
        max_length=255, blank=True
    )  # string representation of coma separated IPs
    tracker = FieldTracker()

    class Meta:
        unique_together = ("service_settings", "backend_id")
        ordering = ["name", "created"]

    @property
    def external_ips(self):
        floating_ips = set(self.floating_ips.values_list("address", flat=True))
        if self.directly_connected_ips:
            floating_ips = floating_ips.union(
                set(self.directly_connected_ips.split(","))
            )
        return list(floating_ips - set(self.internal_ips))

    @property
    def internal_ips(self):
        return [
            val["ip_address"]
            for ip_list in self.ports.values_list("fixed_ips", flat=True)
            for val in ip_list
        ]

    @property
    def size(self):
        return self.volumes.aggregate(models.Sum("size"))["size__sum"]

    @classmethod
    def get_url_name(cls):
        return "openstack-instance"

    def get_log_fields(self):
        return (
            "uuid",
            "name",
            "type",
            "service_settings",
            "project",
            "ram",
            "cores",
        )

    def detect_coordinates(self):
        settings = self.service_settings
        options = settings.options or {}
        if "latitude" in options and "longitude" in options:
            return structure_utils.Coordinates(
                latitude=settings["latitude"], longitude=settings["longitude"]
            )
        else:
            hostname = urlparse(settings.backend_url).hostname
            if hostname:
                return get_coordinates_by_ip(hostname)

    def get_quota_deltas(self):
        return {
            "instances": 1,
            "ram": self.ram,
            "vcpu": self.cores,
        }

    @property
    def floating_ips(self):
        return FloatingIP.objects.filter(port__instance=self)

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "flavor_name",
            "flavor_disk",
            "ram",
            "cores",
            "disk",
            "runtime_state",
            "availability_zone",
            "hypervisor_hostname",
            "directly_connected_ips",
        )

    @classmethod
    def get_online_state(cls):
        return Instance.RuntimeStates.ACTIVE

    @classmethod
    def get_offline_state(cls):
        return Instance.RuntimeStates.SHUTOFF


class Backup(structure_models.SubResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="backups"
    )
    instance = models.ForeignKey(
        Instance, related_name="backups", on_delete=models.CASCADE
    )
    backup_schedule = models.ForeignKey(
        "BackupSchedule",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="backups",
    )
    kept_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Guaranteed time of backup retention. If null - keep forever."),
    )
    metadata = JSONField(
        blank=True,
        help_text=_(
            "Additional information about backup, can be used for backup restoration or deletion"
        ),
    )
    snapshots = models.ManyToManyField("Snapshot", related_name="backups")

    @classmethod
    def get_url_name(cls):
        return "openstack-backup"


class BackupRestoration(core_models.UuidMixin, TimeStampedModel):
    """This model corresponds to instance restoration from backup."""

    backup = models.ForeignKey(
        on_delete=models.CASCADE, to=Backup, related_name="restorations"
    )
    instance = models.OneToOneField(
        Instance, related_name="+", on_delete=models.CASCADE
    )
    flavor = models.ForeignKey(
        Flavor, related_name="+", null=True, blank=True, on_delete=models.SET_NULL
    )

    class Permissions:
        customer_path = "backup__project__customer"
        project_path = "backup__project"


class BaseSchedule(structure_models.BaseResource, core_models.ScheduleMixin):
    retention_time = models.PositiveIntegerField(
        help_text=_("Retention time in days, if 0 - resource will be kept forever")
    )
    maximal_number_of_resources = models.PositiveSmallIntegerField()
    call_count = models.PositiveSmallIntegerField(
        default=0, help_text=_("How many times a resource schedule was called.")
    )

    class Meta:
        abstract = True


class BackupSchedule(BaseSchedule):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="backup_schedules"
    )
    instance = models.ForeignKey(
        on_delete=models.CASCADE, to=Instance, related_name="backup_schedules"
    )

    tracker = FieldTracker()

    def __str__(self):
        return f"BackupSchedule of {self.instance}. Active: {self.is_active}"

    @classmethod
    def get_url_name(cls):
        return "openstack-backup-schedule"


class SnapshotSchedule(BaseSchedule):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="snapshot_schedules"
    )
    source_volume = models.ForeignKey(
        on_delete=models.CASCADE, to=Volume, related_name="snapshot_schedules"
    )

    tracker = FieldTracker()

    def __str__(self):
        return f"SnapshotSchedule of {self.source_volume}. Active: {self.is_active}"

    @classmethod
    def get_url_name(cls):
        return "openstack-snapshot-schedule"
