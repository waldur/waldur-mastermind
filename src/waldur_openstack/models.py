import logging
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from django.core import validators
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, OuterRef, Q, Subquery
from django.template.defaultfilters import slugify
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.fields import JSONField
from waldur_core.core.validators import validate_name
from waldur_core.logging.mixins import LoggableMixin
from waldur_core.quotas import models as quotas_models
from waldur_core.quotas.fields import QuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import filter_queryset_for_user

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager

    from waldur_openstack.backend import OpenStackBackend


logger = logging.getLogger(__name__)

# Octavia LBaaS choices (OVN supports TCP/UDP for protocol and health monitor type)
PROTOCOL_CHOICES = [("TCP", "TCP"), ("UDP", "UDP")]
LB_ALGORITHM_CHOICES = [
    ("ROUND_ROBIN", "Round Robin"),
    ("LEAST_CONNECTIONS", "Least Connections"),
    ("SOURCE_IP", "Source IP"),
    ("SOURCE_IP_PORT", "Source IP port"),
]
HEALTHMONITOR_TYPE_CHOICES = [("TCP", "TCP"), ("UDP", "UDP")]
OVN_SUPPORTED_LB_ALGORITHMS = ["SOURCE_IP_PORT"]


def build_tenants_query(user):
    return Q(tenants__in=filter_queryset_for_user(Tenant.objects.all(), user))


class Tenant(
    core_models.ActionMixin,
    quotas_models.QuotaModelMixin,
    core_models.RuntimeStateMixin,
    structure_models.BaseResource,
    core_models.AvailableMixin,
    core_models.BackendMissingMixin,
):
    flavors: models.Manager["Flavor"]
    images: models.Manager["Image"]
    volume_types: models.Manager["VolumeType"]
    server_groups: models.Manager["ServerGroup"]
    security_groups: models.Manager["SecurityGroup"]
    floating_ips: models.Manager["FloatingIP"]
    routers: models.Manager["Router"]
    load_balancers: models.Manager["LoadBalancer"]
    networks: models.Manager["Network"]
    ports: models.Manager["Port"]
    volume_availability_zones: models.Manager["VolumeAvailabilityZone"]
    volumes: models.Manager["Volume"]
    snapshots: models.Manager["Snapshot"]
    instance_availability_zones: models.Manager["InstanceAvailabilityZone"]
    instances: models.Manager["Instance"]
    backups: models.Manager["Backup"]
    network_rbac_policies: models.Manager["NetworkRBACPolicy"]
    id: int

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
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("ID of tenant in the OpenStack backend"),
    )

    internal_network_id = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("ID of internal network in OpenStack tenant"),
    )
    external_network_id = models.CharField(
        max_length=64,
        blank=True,
        help_text=_("ID of external network connected to OpenStack tenant"),
    )
    external_network_ref = models.ForeignKey(
        "ExternalNetwork",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tenants",
        help_text=_("External network connected to OpenStack tenant"),
    )

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
    user_username = models.CharField(
        max_length=50, blank=True, help_text=_("Username of the tenant user")
    )
    user_password = models.CharField(
        max_length=50, blank=True, help_text=_("Password of the tenant user")
    )
    update_triggered = models.DateTimeField(
        blank=True,
        null=True,
        help_text=_("Timestamp of when tenant update was last triggered"),
    )
    skip_creation_of_default_router = models.BooleanField(
        default=False,
        help_text=_("If True, default router will not be created for this tenant"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta(structure_models.BaseResource.Meta):
        unique_together = ("service_settings", "backend_id")

    @classmethod
    def generate_username(cls, name):
        """
        Generates random valid tenant user name based on tenant name
        :param name: tenant name
        :return: username
        """
        return slugify(name)[:25] + "-user-%s" % core_utils.pwgen(4)

    def get_backend(self) -> "OpenStackBackend":
        return self.service_settings.get_backend()

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "name",
            "description",
            "error_message",
            "runtime_state",
        )

    def get_access_url(self) -> str | None:
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

    @property
    def available_subnets(self):
        # Subnets directly belonging to the tenant
        tenant_subnets = SubNet.objects.filter(tenant=self)
        # Subnets from networks shared with the tenant via RBAC
        shared_network_ids = NetworkRBACPolicy.objects.filter(
            target_tenant=self
        ).values_list("network_id", flat=True)
        shared_subnets = SubNet.objects.filter(network_id__in=shared_network_ids)
        # Combine both
        return (tenant_subnets | shared_subnets).distinct()

    @property
    def available_networks(self):
        tenant_networks = Network.objects.filter(tenant=self)
        shared_network_ids = NetworkRBACPolicy.objects.filter(
            target_tenant=self
        ).values_list("network_id", flat=True)
        shared_networks = Network.objects.filter(id__in=shared_network_ids)
        return (tenant_networks | shared_networks).distinct()

    @property
    def available_ports(self):
        tenant_ports = Port.objects.filter(tenant=self)
        shared_network_ids = NetworkRBACPolicy.objects.filter(
            target_tenant=self
        ).values_list("network_id", flat=True)
        shared_ports = Port.objects.filter(network_id__in=shared_network_ids)
        return (tenant_ports | shared_ports).distinct()


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


class ImageManager(models.Manager):
    def get_queryset(self):
        base_qs = models.QuerySet(self.model, using=self._db)
        latest_id = (
            base_qs.filter(name=OuterRef("name"), settings=OuterRef("settings"))
            .order_by(
                F("backend_created_at").desc(nulls_last=True),
                "-id",
            )
            .values("id")[:1]
        )
        return base_qs.filter(id=Subquery(latest_id))


class Image(structure_models.ServiceProperty):
    objects = ImageManager()
    all_objects = models.Manager()
    min_disk = models.PositiveIntegerField(
        default=0, help_text=_("Minimum disk size in MiB")
    )
    min_ram = models.PositiveIntegerField(
        default=0, help_text=_("Minimum memory size in MiB")
    )
    backend_created_at = models.DateTimeField(null=True, blank=True)
    tenants = models.ManyToManyField(to=Tenant, related_name="images")

    # Glance custom properties used to identify "stable device rescue" images.
    # An image with either property set may be used as a Nova rescue image;
    # volume-backed instances *require* such a tagged image — the legacy
    # rescue path does not support BFV instances and Nova will leave the
    # instance in unrecoverable ERROR state without one.
    hw_rescue_device = models.CharField(
        max_length=20,
        blank=True,
        help_text=_("Glance hw_rescue_device property (cdrom/disk/floppy)."),
    )
    hw_rescue_bus = models.CharField(
        max_length=20,
        blank=True,
        help_text=_("Glance hw_rescue_bus property (scsi/virtio/ide/usb)."),
    )

    class Permissions:
        build_query = build_tenants_query

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "min_disk",
            "min_ram",
            "hw_rescue_device",
            "hw_rescue_bus",
        )

    @classmethod
    def get_url_name(cls):
        return "openstack-image"

    @property
    def is_rescue_image(self) -> bool:
        return bool(self.hw_rescue_device or self.hw_rescue_bus)


class VolumeType(core_models.DescribableMixin, structure_models.ServiceProperty):
    tenants = models.ManyToManyField(to=Tenant, related_name="volume_types")
    disabled = models.BooleanField(default=False)

    class Meta:
        unique_together = ("settings", "backend_id")

    class Permissions:
        build_query = build_tenants_query

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "openstack-volume-type"


class ExternalNetwork(core_models.DescribableMixin, structure_models.ServiceProperty):
    """Provider-level external network discovered from OpenStack."""

    is_shared = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)
    status = models.CharField(max_length=30, blank=True)

    class Meta:
        unique_together = ("settings", "backend_id")

    @classmethod
    def get_url_name(cls):
        return "openstack-external-network"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "is_shared",
            "is_default",
            "status",
            "description",
        )

    def get_backend(self):
        return self.settings.get_backend()


class Trait(core_models.UuidMixin, models.Model):
    """OpenStack Placement trait — a capability flag on a resource provider.

    Examples: HW_CPU_X86_AVX2, STORAGE_DISK_SSD, COMPUTE_ACCELERATORS,
    HW_GPU_API_VULKAN, CUSTOM_*.

    This is a global catalog keyed by ``name``: standard traits (the os-traits
    catalog) mean the same thing on every cloud, so deduplicating them across
    all ServiceSettings is correct. CUSTOM_* traits, however, are operator-
    defined and are only guaranteed to be meaningful within a single source /
    ServiceSettings — two unrelated clouds may both define CUSTOM_GOLD_TIER
    with different semantics. Because hypervisor queries are normally scoped by
    settings_uuid this is harmless in practice, but filtering hypervisors by a
    CUSTOM_* trait WITHOUT scoping by settings is not semantically safe.
    """

    name = models.CharField(max_length=255, unique=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Hypervisor(structure_models.ServiceProperty):
    """OpenStack hypervisor node pulled from Nova admin API.

    Visible to staff, support, and service provider owners/managers only.
    """

    class Permissions:
        customer_path = "settings__customer"

    traits = models.ManyToManyField(
        Trait,
        related_name="hypervisors",
        blank=True,
        help_text=_("Placement traits (capability flags) reported for this host."),
    )

    hypervisor_type = models.CharField(
        max_length=50,
        blank=True,
        help_text=_("Hypervisor type, e.g. KVM, QEMU, VMware"),
    )
    vcpus = models.PositiveIntegerField(default=0, help_text=_("Total vCPUs"))
    vcpus_used = models.PositiveIntegerField(default=0, help_text=_("Used vCPUs"))
    memory_mb = models.PositiveIntegerField(default=0, help_text=_("Total RAM in MiB"))
    memory_mb_used = models.PositiveIntegerField(
        default=0, help_text=_("Used RAM in MiB")
    )
    local_gb = models.PositiveIntegerField(default=0, help_text=_("Total disk in GiB"))
    local_gb_used = models.PositiveIntegerField(
        default=0, help_text=_("Used disk in GiB")
    )
    running_vms = models.PositiveIntegerField(
        default=0, help_text=_("Number of running VMs")
    )
    state = models.CharField(
        max_length=50, blank=True, help_text=_("Hypervisor state, e.g. up or down")
    )
    status = models.CharField(
        max_length=50,
        blank=True,
        help_text=_("Hypervisor status, e.g. enabled or disabled"),
    )

    @classmethod
    def get_url_name(cls):
        return "openstack-hypervisor"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "hypervisor_type",
            "vcpus",
            "vcpus_used",
            "memory_mb",
            "memory_mb_used",
            "local_gb",
            "local_gb_used",
            "running_vms",
            "state",
            "status",
        )


class HypervisorInventory(core_models.UuidMixin, models.Model):
    """One Placement inventory line per (hypervisor, resource_class).

    Stores the *raw* Placement values (total, reserved, allocation_ratio, used)
    so admins can explain quota math and so non-classic resource classes
    (VGPU, IPV4_ADDRESS, NUMA_CORE, CUSTOM_*, …) are surfaced — the parent
    Hypervisor still keeps the legacy `vcpus`/`memory_mb`/`local_gb` columns
    populated with the effective totals for backward compat.
    """

    class Permissions:
        # Visible to staff/support and to service-provider owners.
        customer_path = "hypervisor__settings__customer"

    hypervisor = models.ForeignKey(
        Hypervisor,
        on_delete=models.CASCADE,
        related_name="inventories",
    )
    resource_class = models.CharField(
        max_length=255,
        help_text=_(
            "Placement resource class, e.g. VCPU, MEMORY_MB, DISK_GB, VGPU, "
            "PCI_DEVICE, NUMA_CORE, CUSTOM_*."
        ),
    )
    total = models.PositiveBigIntegerField(default=0)
    reserved = models.PositiveBigIntegerField(default=0)
    allocation_ratio = models.FloatField(default=1.0)
    used = models.PositiveBigIntegerField(default=0)

    class Meta:
        unique_together = ("hypervisor", "resource_class")
        ordering = ["hypervisor", "resource_class", "id"]

    @classmethod
    def get_url_name(cls):
        return "openstack-hypervisor-inventory"

    def __str__(self):
        return f"{self.hypervisor.name}:{self.resource_class}"

    @property
    def effective_total(self) -> int:
        """Capacity the Nova scheduler treats as available."""
        return int(max(self.total - self.reserved, 0) * (self.allocation_ratio or 1.0))


class ExternalSubnet(
    core_models.DescribableMixin,
    core_models.UuidMixin,
    core_models.BackendModelMixin,
    core_models.NameMixin,
    models.Model,
):
    """Subnet within a provider-level external network."""

    network = models.ForeignKey(
        ExternalNetwork,
        on_delete=models.CASCADE,
        related_name="subnets",
    )
    backend_id = models.CharField(max_length=255, db_index=True)
    cidr = models.CharField(max_length=32, blank=True)
    gateway_ip = models.GenericIPAddressField(null=True, blank=True)
    ip_version = models.SmallIntegerField(default=4)
    enable_dhcp = models.BooleanField(default=True)
    allocation_pools = models.JSONField(default=list, blank=True)
    dns_nameservers = models.JSONField(default=list, blank=True)
    public_ip_range = models.CharField(
        max_length=32,
        blank=True,
        help_text=_(
            "Public CIDR mapped to this subnet (for carrier-grade NAT overlay)"
        ),
    )

    class Meta:
        unique_together = ("network", "backend_id")

    def __str__(self):
        return f"{self.name} ({self.cidr})"

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "backend_id",
            "name",
            "cidr",
            "gateway_ip",
            "ip_version",
            "enable_dhcp",
            "allocation_pools",
            "dns_nameservers",
            "description",
        )


class ServerGroup(structure_models.BaseResource):
    AFFINITY = "affinity"
    ANTI_AFFINITY = "anti-affinity"
    SOFT_AFFINITY = "soft-affinity"
    SOFT_ANTI_AFFINITY = "soft-anti-affinity"

    POLICIES = (
        (AFFINITY, "Affinity"),
        (ANTI_AFFINITY, "Anti-affinity"),
        (SOFT_AFFINITY, "Soft affinity"),
        (SOFT_ANTI_AFFINITY, "Soft anti-affinity"),
    )

    policy = models.CharField(
        max_length=40,
        blank=True,
        choices=POLICIES,
        help_text=_(
            "Server group policy determining the rules for scheduling servers in this group"
        ),
    )

    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name="server_groups"
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

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


class SecurityGroup(structure_models.BaseResource):
    rules: models.Manager["SecurityGroupRule"]
    ports: models.Manager["Port"]
    instances: models.Manager["Instance"]
    id: int

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


def validate_security_group_rule_protocol(value):
    """Accept "", named protocols (tcp/udp/icmp) or an IANA protocol number 0-255."""
    if value in ("", "tcp", "udp", "icmp"):
        return
    if value.isdigit() and 0 <= int(value) <= 255:
        return
    raise ValidationError(
        _(
            'Protocol must be one of "tcp", "udp", "icmp", empty (any) '
            "or an IANA protocol number between 0 and 255, got %(value)r."
        ),
        params={"value": value},
    )


class BaseSecurityGroupRule(core_models.DescribableMixin, models.Model):
    class Meta:
        abstract = True

    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"

    NAMED_PROTOCOLS = (TCP, UDP, ICMP)

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

    protocol = models.CharField(
        max_length=40,
        blank=True,
        validators=[validate_security_group_rule_protocol],
        help_text=_(
            "Network protocol: 'tcp', 'udp', 'icmp', empty (any) "
            "or an IANA protocol number 0-255 (e.g. '112' for VRRP)."
        ),
    )
    from_port = models.IntegerField(
        validators=[validators.MaxValueValidator(65535)],
        null=True,
        help_text=_("Starting port number in the range (1-65535)"),
    )
    to_port = models.IntegerField(
        validators=[validators.MaxValueValidator(65535)],
        null=True,
        help_text=_("Ending port number in the range (1-65535)"),
    )
    cidr = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("CIDR notation for the source/destination network address range"),
    )
    direction = models.CharField(
        max_length=8,
        default=INGRESS,
        choices=DIRECTIONS,
        help_text=_(
            "Traffic direction - either 'ingress' (incoming) or 'egress' (outgoing)"
        ),
    )
    ethertype = models.CharField(
        max_length=40,
        default=IPv4,
        choices=ETHER_TYPES,
        help_text=_("IP protocol version - either 'IPv4' or 'IPv6'"),
    )
    backend_id = models.CharField(
        max_length=36,
        blank=True,
        help_text=_("ID of the security group rule in the OpenStack backend"),
    )


class SecurityGroupRule(BaseSecurityGroupRule, LoggableMixin):
    remote_group_id: int | None
    security_group_id: int

    def __str__(self):
        return f"{self.security_group} ({self.protocol}): {self.cidr} ({self.from_port} -> {self.to_port})"

    security_group = models.ForeignKey(
        on_delete=models.CASCADE,
        to=SecurityGroup,
        related_name="rules",
        help_text=_("Security group this rule belongs to"),
    )
    remote_group = models.ForeignKey(
        on_delete=models.CASCADE,
        to=SecurityGroup,
        related_name="+",
        null=True,
        blank=True,
        help_text=_("Remote security group that this rule references, if any"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

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


class FloatingIP(core_models.RuntimeStateMixin, structure_models.BaseResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="floating_ips",
        help_text=_("OpenStack tenant this floating IP belongs to"),
    )
    address = models.GenericIPAddressField(
        null=True,
        blank=True,
        protocol="IPv4",
        default=None,
        help_text=_("The public IPv4 address of the floating IP"),
    )
    external_address = models.GenericIPAddressField(
        editable=False,
        null=True,
        help_text=_(
            "Optional address that maps to floating IP's address in external networks"
        ),
    )

    backend_network_id = models.CharField(
        max_length=255,
        editable=False,
        help_text=_("ID of network in OpenStack where this floating IP is allocated"),
    )
    port = models.ForeignKey["Port"](
        on_delete=models.SET_NULL,
        to="Port",
        related_name="floating_ips",
        blank=True,
        null=True,
        help_text=_("OpenStack port this floating IP is associated with"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta(structure_models.BaseResource.Meta):
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


class Router(structure_models.BaseResource):
    class Meta(structure_models.BaseResource.Meta):
        unique_together = [["tenant", "backend_id"]]

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="routers",
        help_text=_("OpenStack tenant this router belongs to"),
    )
    backend_id = models.CharField(
        max_length=255, blank=True, null=True, help_text=_("Router ID in OpenStack")
    )
    routes = JSONField(
        default=list, help_text=_("List of routes configured on the router")
    )
    fixed_ips = JSONField(
        default=list,
        help_text=_("List of fixed IP addresses assigned to the router interfaces"),
    )
    ports = models.ManyToManyField(
        "Port",
        related_name="routers",
        blank=True,
        help_text=_("Network ports attached to this router"),
    )
    external_network_id = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Backend ID of the external network used as gateway"),
    )
    external_network_ref = models.ForeignKey(
        "ExternalNetwork",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="routers",
        help_text=_(
            "Reference to ExternalNetwork if gateway is a global external network"
        ),
    )
    enable_snat = models.BooleanField(
        null=True,
        default=None,
        help_text=_(
            "Whether SNAT is enabled on the external gateway. None means OpenStack default (True)."
        ),
    )
    external_fixed_ips = JSONField(
        default=list,
        help_text=_("List of fixed IP addresses on the external gateway port"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    @property
    def has_external_gateway(self):
        return bool(self.external_network_id)

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-router"


class LoadBalancer(structure_models.BaseResource):
    class Meta(structure_models.BaseResource.Meta):
        unique_together = [["tenant", "backend_id"]]

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="load_balancers",
        help_text=_("OpenStack tenant this load balancer belongs to"),
    )
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Load balancer ID in Octavia"),
    )
    vip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        protocol="IPv4",
        help_text=_("Virtual IP address of the load balancer"),
    )
    vip_subnet = models.ForeignKey(
        on_delete=models.SET_NULL,
        to="SubNet",
        null=True,
        blank=False,
        help_text=_("Subnet for the load balancer VIP"),
    )
    provider = models.CharField(
        max_length=64,
        default="ovn",
        editable=False,
        help_text=_("Octavia provider (e.g. ovn for OVN LBaaS)"),
    )
    provisioning_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia provisioning status: ACTIVE, PENDING_CREATE, etc."),
    )
    operating_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia operating status: ONLINE, OFFLINE, etc."),
    )
    vip_port = models.ForeignKey(
        on_delete=models.SET_NULL,
        to="Port",
        related_name="+",
        null=True,
        blank=True,
        editable=False,
        help_text=_("Neutron VIP port in Waldur (for floating IP and security groups)"),
    )
    attached_floating_ip = models.ForeignKey(
        on_delete=models.SET_NULL,
        to="FloatingIP",
        related_name="load_balancers",
        null=True,
        blank=True,
        help_text=_("Floating IP attached to the VIP port"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-loadbalancer"


class Pool(structure_models.BaseResource):
    """Octavia LBaaS backend pool."""

    class Meta(structure_models.BaseResource.Meta):
        unique_together = [["load_balancer", "backend_id"]]

    load_balancer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=LoadBalancer,
        related_name="pools",
        help_text=_("Load balancer this pool belongs to"),
    )
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Pool ID in Octavia"),
    )
    protocol = models.CharField(
        max_length=16,
        choices=PROTOCOL_CHOICES,
        help_text=_("Protocol for the pool: TCP, UDP (OVN supports TCP and UDP)"),
    )
    lb_algorithm = models.CharField(
        max_length=32,
        default="SOURCE_IP_PORT",
        choices=LB_ALGORITHM_CHOICES,
        help_text=_("Load balancing algorithm."),
    )
    provisioning_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia provisioning status: ACTIVE, PENDING_CREATE, etc."),
    )
    operating_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia operating status: ONLINE, OFFLINE, etc."),
    )

    def get_backend(self):
        return self.load_balancer.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-pool"


class Listener(structure_models.BaseResource):
    """Octavia LBaaS listener. Listens on load balancer VIP, forwards to default pool."""

    class Meta(structure_models.BaseResource.Meta):
        unique_together = [["load_balancer", "backend_id"]]

    load_balancer = models.ForeignKey(
        on_delete=models.CASCADE,
        to=LoadBalancer,
        related_name="listeners",
        help_text=_("Load balancer this listener belongs to"),
    )
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Listener ID in Octavia"),
    )
    protocol = models.CharField(
        max_length=16,
        choices=PROTOCOL_CHOICES,
        help_text=_("Protocol for the listener: TCP, UDP (OVN supports TCP and UDP)"),
    )
    protocol_port = models.IntegerField(
        validators=[
            validators.MinValueValidator(1),
            validators.MaxValueValidator(65535),
        ],
        help_text=_("Port on which the listener listens"),
    )
    default_pool = models.ForeignKey(
        on_delete=models.SET_NULL,
        to=Pool,
        related_name="listeners",
        help_text=_("Default pool for this listener"),
        null=True,
        blank=True,
    )
    provisioning_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia provisioning status: ACTIVE, PENDING_CREATE, etc."),
    )
    operating_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia operating status: ONLINE, OFFLINE, etc."),
    )

    def get_backend(self):
        return self.load_balancer.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-listener"


class PoolMember(structure_models.BaseResource):
    """Octavia LBaaS pool member. Represents a backend server in a pool."""

    class Meta(structure_models.BaseResource.Meta):
        unique_together = [["pool", "backend_id"]]

    pool = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Pool,
        related_name="members",
        help_text=_("Pool this member belongs to"),
    )
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Member ID in Octavia"),
    )
    address = models.GenericIPAddressField(
        help_text=_("IP address of the backend server"),
    )
    protocol_port = models.PositiveIntegerField(
        validators=[
            validators.MinValueValidator(1),
            validators.MaxValueValidator(65535),
        ],
        help_text=_("Port on the backend server"),
    )
    subnet = models.ForeignKey(
        on_delete=models.PROTECT,
        to="SubNet",
        related_name="+",
        null=True,
        blank=True,
        help_text=_("Neutron subnet for the member (same tenant as the load balancer)"),
    )
    weight = models.PositiveSmallIntegerField(
        default=1,
        help_text=_("Weight for load balancing (1-256)"),
    )
    provisioning_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia provisioning status: ACTIVE, PENDING_CREATE, etc."),
    )
    operating_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia operating status: ONLINE, OFFLINE, etc."),
    )

    def get_backend(self):
        return self.pool.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-poolmember"


class HealthMonitor(structure_models.BaseResource):
    """Octavia LBaaS health monitor. One per pool. OVN supports TCP and UDP only."""

    pool = models.OneToOneField(
        on_delete=models.CASCADE,
        to=Pool,
        related_name="health_monitor",
        help_text=_("Pool this health monitor belongs to"),
    )
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Health monitor ID in Octavia"),
    )
    monitor_type = models.CharField(
        max_length=16,
        choices=HEALTHMONITOR_TYPE_CHOICES,
        help_text=_("Health check type: TCP, UDP (OVN supports TCP and UDP only)"),
        db_column="type",
    )
    max_retries_down = models.PositiveIntegerField(default=3)
    delay = models.PositiveIntegerField(
        help_text=_("Interval between health checks in seconds"), default=5
    )
    timeout = models.PositiveIntegerField(
        help_text=_("Time in seconds to timeout a health check"), default=5
    )
    max_retries = models.PositiveIntegerField(default=3)
    provisioning_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia provisioning status: ACTIVE, PENDING_CREATE, etc."),
    )
    operating_status = models.CharField(
        max_length=32,
        blank=True,
        editable=False,
        help_text=_("Octavia operating status: ONLINE, OFFLINE, etc."),
    )

    def get_backend(self):
        return self.pool.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-healthmonitor"


class Network(core_models.RuntimeStateMixin, structure_models.BaseResource):
    class Meta(structure_models.BaseResource.Meta):
        pass

    subnets: models.Manager["SubNet"]
    ports: models.Manager["Port"]
    rbac_policies: models.Manager["NetworkRBACPolicy"]

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="networks",
        help_text=_("OpenStack tenant this network belongs to"),
    )
    is_external = models.BooleanField(
        default=False,
        help_text=_(
            "Defines whether this network is external (public) or internal (private)"
        ),
    )
    type = models.CharField(
        max_length=50,
        blank=True,
        help_text=_("Network type, such as local, flat, vlan, vxlan, or gre"),
    )
    segmentation_id = models.IntegerField(
        null=True,
        help_text=_("VLAN ID for VLAN networks or tunnel ID for VXLAN/GRE networks"),
    )
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
    port_security_enabled = models.BooleanField(
        default=True,
        help_text=_(
            "Default port_security_enabled for ports on this network. "
            "When False, ports created on this network inherit disabled "
            "port security unless explicitly overridden."
        ),
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
            "port_security_enabled",
        )


class SubNet(structure_models.BaseResource):
    ports: models.Manager["Port"]

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="+",
        help_text=_("OpenStack tenant this subnet belongs to"),
    )
    network = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Network,
        related_name="subnets",
        help_text=_("Network to which this subnet belongs"),
    )
    disable_gateway = models.BooleanField(
        default=False, help_text=_("If True, no gateway IP address will be allocated")
    )
    host_routes = JSONField(
        default=list,
        help_text=_("List of additional routes for the subnet."),
    )
    cidr = models.CharField(
        max_length=32,
        blank=True,
        help_text=_("IPv4 network address in CIDR format (e.g. 192.168.0.0/24)"),
    )
    gateway_ip = models.GenericIPAddressField(
        protocol="IPv4",
        null=True,
        help_text=_("IP address of the gateway for this subnet"),
    )
    allocation_pools = cast(
        list[dict[str, str]],
        JSONField(
            default=dict,
            help_text=_("List of IP ranges available for allocation in this subnet"),
        ),
    )
    ip_version = models.SmallIntegerField(
        default=4, help_text=_("IP protocol version (4 or 6)")
    )
    enable_dhcp = models.BooleanField(
        default=True,
        help_text=_("If True, DHCP service will be enabled on this subnet"),
    )
    dns_nameservers = JSONField(
        default=list,
        help_text=_("List of DNS name servers associated with the subnet."),
    )
    is_connected = models.BooleanField(
        default=True, help_text=_("Is subnet connected to the default tenant router.")
    )

    class Meta(structure_models.BaseResource.Meta):
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


class Port(structure_models.BaseResource):
    class Meta(structure_models.BaseResource.Meta):
        unique_together = [["tenant", "backend_id"]]

    floating_ips: models.Manager["FloatingIP"]

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="ports",
        help_text=_("OpenStack tenant this port belongs to"),
    )
    network = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Network,
        related_name="ports",
        null=True,
        blank=True,
        help_text=_("Network to which this port belongs"),
    )
    port_security_enabled = models.BooleanField(
        default=True,
        help_text=_("If True, security groups and rules will be applied to this port"),
    )
    security_groups = models.ManyToManyField(
        SecurityGroup,
        related_name="ports",
        help_text=_("Security groups associated with this port"),
    )
    instance = models.ForeignKey["Instance"](
        on_delete=models.CASCADE,
        to="Instance",
        related_name="ports",
        null=True,
        blank=True,
        help_text=_("Instance to which this port is attached"),
    )
    subnet = models.ForeignKey(
        on_delete=models.CASCADE,
        to=SubNet,
        related_name="ports",
        null=True,
        blank=True,
        help_text=_("Subnet to which this port belongs"),
    )
    tracker = cast(FieldInstanceTracker, FieldTracker())
    # TODO: Use dedicated field: https://github.com/django-macaddress/django-macaddress
    mac_address = models.CharField(
        max_length=32, blank=True, help_text=_("MAC address of the port")
    )
    fixed_ips = JSONField(
        default=list,
        help_text=_(
            "A list of tuples (ip_address, subnet_id), where ip_address can be both IPv4 and IPv6 "
            "and subnet_id is a backend id of the subnet"
        ),
    )
    backend_id = models.CharField(
        max_length=255, blank=True, null=True, help_text=_("Port ID in OpenStack")
    )
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
        help_text=_(
            "ID of device (instance, router etc) to which this port is connected"
        ),
    )
    device_owner = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text=_("Entity that uses this port (e.g. network:router_interface)"),
    )
    admin_state_up = models.BooleanField(
        blank=True,
        null=True,
        help_text=_(
            "Administrative state of the port. If down, port does not forward packets"
        ),
    )
    status = models.CharField(
        max_length=30,
        blank=True,
        null=True,
        help_text=_("Port status in OpenStack (e.g. ACTIVE, DOWN)"),
    )

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + (
            "fixed_ips",
            "mac_address",
            "allowed_address_pairs",
            "device_id",
            "device_owner",
            "admin_state_up",
            "name",
            "description",
            "status",
        )

    @classmethod
    def get_url_name(cls):
        return "openstack-port"

    def __str__(self):
        """
        Return a string representation of the port.

        If fixed_ips exists and has IP addresses, return a comma-separated list of IP addresses.
        Otherwise, return a generic representation with the name and ID.
        """
        if self.fixed_ips:
            ips = []
            for fixed_ip in self.fixed_ips:
                ip_address = (
                    fixed_ip.get("ip_address") if isinstance(fixed_ip, dict) else None
                )
                if ip_address:
                    ips.append(ip_address)

            if ips:
                return ",".join(ips)

        # Fallback if there are no fixed_ips, or they don't have ip_address
        if self.name:
            return f"Port {self.name}"
        else:
            return f"Port {self.uuid.hex}"


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
    external_network_ref = models.ForeignKey(
        ExternalNetwork,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_openstack_settings",
        help_text=_("External network for this customer"),
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
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="volume_availability_zones",
        null=True,
        blank=True,
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


class Volume(
    core_models.ActionMixin,
    TenantQuotaMixin,
    structure_models.Storage,
    core_models.AvailableMixin,
    core_models.BackendMissingMixin,
):
    snapshots: models.Manager["Snapshot"]
    restoration: models.Manager["SnapshotRestoration"]

    # Cinder allows volume names up to 255 chars, but the base NameMixin caps at
    # 150. Widen so volumes with long backend names can be imported (WAL-10102).
    name = models.CharField(_("name"), max_length=255, validators=[validate_name])

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="volumes",
        help_text=_("OpenStack tenant this volume belongs to"),
    )
    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Volume ID in the OpenStack backend"),
    )

    instance = models.ForeignKey["Instance"](
        on_delete=models.CASCADE,
        to="Instance",
        related_name="volumes",
        blank=True,
        null=True,
        help_text=_("Instance that this volume is attached to, if any"),
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
    bootable = models.BooleanField(
        default=False,
        help_text=_("Indicates if this volume can be used to boot an instance"),
    )
    metadata = JSONField(
        blank=True, help_text=_("Arbitrary key-value pairs associated with the volume")
    )
    image = models.ForeignKey(
        Image,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text=_("Image that this volume was created from, if any"),
    )
    image_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Name of the image this volume was created from"),
    )
    image_metadata = JSONField(
        blank=True, help_text=_("Metadata of the image this volume was created from")
    )
    type = models.ForeignKey(
        VolumeType,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text=_("Type of the volume (e.g. SSD, HDD)"),
    )
    availability_zone = models.ForeignKey(
        VolumeAvailabilityZone,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text=_("Availability zone where this volume is located"),
    )
    source_snapshot = models.ForeignKey(
        "Snapshot",
        related_name="volumes",
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text=_("Snapshot that this volume was created from, if any"),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta(structure_models.BaseResource.Meta):
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

    @property
    def extend_enabled(self):
        from waldur_openstack import utils

        try:
            utils.check_volume_resize_enabled(self)
            return True
        except core_exceptions.IncorrectStateException:
            return False


class Snapshot(
    core_models.ActionMixin,
    TenantQuotaMixin,
    structure_models.Storage,
    core_models.BackendMissingMixin,
):
    volumes: models.Manager["Volume"]
    restorations: models.Manager["SnapshotRestoration"]
    backups: models.Manager["Backup"]

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="snapshots",
        help_text=_("OpenStack tenant this snapshot belongs to"),
    )
    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Snapshot ID in the OpenStack backend"),
    )

    source_volume = models.ForeignKey(
        Volume,
        related_name="snapshots",
        null=True,
        on_delete=models.CASCADE,
        help_text=_("Volume from which this snapshot was created"),
    )
    metadata = JSONField(
        blank=True,
        help_text=_(
            "Additional information about the snapshot stored as key-value pairs"
        ),
    )

    tracker = cast(FieldInstanceTracker, FieldTracker())

    kept_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Guaranteed time of snapshot retention. If null - keep forever."),
    )

    class Meta(structure_models.BaseResource.Meta):
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
        on_delete=models.CASCADE,
        to=Snapshot,
        related_name="restorations",
        help_text=_("Snapshot from which the volume is being restored"),
    )
    volume = models.OneToOneField(
        Volume,
        related_name="restoration",
        on_delete=models.CASCADE,
        help_text=_("Volume that is being restored from the snapshot"),
    )

    class Permissions:
        customer_path = "snapshot__project__customer"
        project_path = "snapshot__project"


class InstanceAvailabilityZone(structure_models.BaseServiceProperty):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="instance_availability_zones",
        help_text=_("OpenStack tenant this availability zone belongs to"),
        null=True,
        blank=True,
    )
    settings = models.ForeignKey(
        on_delete=models.CASCADE,
        to=structure_models.ServiceSettings,
        related_name="+",
        help_text=_("Service settings for this availability zone"),
    )
    available = models.BooleanField(
        default=True,
        help_text=_(
            "Indicates whether this availability zone is available for instance provisioning"
        ),
    )

    class Meta:
        unique_together = ("settings", "name")

    def __str__(self):
        return self.name

    @classmethod
    def get_url_name(cls):
        return "openstack-instance-availability-zone"


class Instance(
    core_models.ActionMixin,
    TenantQuotaMixin,
    structure_models.VirtualMachine,
    core_models.AvailableMixin,
    core_models.BackendMissingMixin,
):
    tracker = cast(FieldInstanceTracker, FieldTracker())

    id: int
    ports: "RelatedManager[Port]"
    volumes: "RelatedManager[Volume]"
    backups: "RelatedManager[Backup]"

    # Nova allows server display names and image names up to 255 chars, but the
    # base NameMixin/VirtualMachine cap both at 150. Widen so instances with long
    # backend names can be imported instead of crashing tenant sync (WAL-10102).
    name = models.CharField(_("name"), max_length=255, validators=[validate_name])
    image_name = models.CharField(max_length=255, blank=True)

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
        # Nova returns "RESCUE" as the top-level server status when an
        # instance is in rescue mode (vm_state="rescued" is a separate
        # internal field). instance.runtime_state is set verbatim from
        # backend_instance.status, so we match Nova's casing here.
        RESCUE = "RESCUE"
        RESIZED = "RESIZED"
        REVERT_RESIZE = "REVERT_RESIZE"
        SHUTOFF = "SHUTOFF"
        STOPPED = "STOPPED"
        SUSPENDED = "SUSPENDED"
        VERIFY_RESIZE = "VERIFY_RESIZE"

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="instances",
        help_text=_("OpenStack tenant this instance belongs to"),
    )
    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Instance ID in the OpenStack backend"),
    )

    availability_zone = models.ForeignKey(
        InstanceAvailabilityZone,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text=_("Availability zone where this instance is located"),
    )
    flavor_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Name of the flavor used by this instance"),
    )
    flavor_disk = models.PositiveIntegerField(
        default=0, help_text=_("Flavor disk size in MiB")
    )
    security_groups = models.ManyToManyField(
        SecurityGroup,
        related_name="instances",
        blank=True,
        help_text=_("Security groups attached to this instance"),
    )
    server_group = models.ForeignKey(
        ServerGroup,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        help_text=_("Server group this instance belongs to"),
    )
    subnets = models.ManyToManyField(
        SubNet,
        through=Port,
        help_text=_("Subnets connected to this instance through ports"),
    )
    hypervisor_hostname = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Name of the hypervisor hosting this instance"),
    )

    connect_directly_to_external_network = models.BooleanField(
        default=False,
        help_text=_("If True, instance will be connected directly to external network"),
    )
    directly_connected_ips = models.TextField(
        blank=True,
        help_text=_("Comma-separated list of directly connected IP addresses"),
    )
    config_drive = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "Force config drive on or off for this instance. "
            "If null, the tenant-wide default from service settings is used."
        ),
    )
    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta(structure_models.BaseResource.Meta):
        unique_together = ("service_settings", "backend_id")
        ordering = ["name", "created", "id"]

    @property
    def external_ips(self) -> list[str]:
        floating_ips = set(self.floating_ips.values_list("address", flat=True))
        if self.directly_connected_ips:
            floating_ips = floating_ips.union(
                set(self.directly_connected_ips.split(","))
            )
        return (
            list(floating_ips - set(self.internal_ips))
            if self.internal_ips
            else list(floating_ips)
        )

    @property
    def external_address(self) -> set[str]:
        return set(
            self.floating_ips.exclude(external_address__isnull=True).values_list(
                "external_address", flat=True
            )
        )

    @property
    def internal_ips(self):
        internal_ips = set()
        for ip_list in self.ports.values_list("fixed_ips", flat=True):
            if ip_list:
                internal_ips.update({val["ip_address"] for val in ip_list})
        return list(internal_ips)

    @property
    def size(self) -> int:
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

    def get_quota_deltas(self):
        return {
            "instances": 1,
            "ram": self.ram,
            "vcpu": self.cores,
        }

    @property
    def floating_ips(self) -> models.QuerySet[FloatingIP]:
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
            "image_name",
        )

    @classmethod
    def get_online_state(cls):
        return Instance.RuntimeStates.ACTIVE

    @classmethod
    def get_offline_state(cls):
        return Instance.RuntimeStates.SHUTOFF


class Backup(structure_models.BaseResource):
    restorations: models.Manager["BackupRestoration"]

    tenant = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Tenant,
        related_name="backups",
        help_text=_("OpenStack tenant this backup belongs to"),
    )
    instance = models.ForeignKey(
        Instance,
        related_name="backups",
        on_delete=models.CASCADE,
        help_text=_("Instance that this backup is created from"),
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
    snapshots = models.ManyToManyField(
        "Snapshot",
        related_name="backups",
        help_text=_("Snapshots that comprise this backup"),
    )

    @classmethod
    def get_url_name(cls):
        return "openstack-backup"


class BackupRestoration(core_models.UuidMixin, TimeStampedModel):
    """This model represents an instance restoration from a backup."""

    backup = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Backup,
        related_name="restorations",
        help_text=_("Backup from which the instance is being restored"),
    )
    instance = models.OneToOneField(
        Instance,
        related_name="+",
        on_delete=models.CASCADE,
        help_text=_("Instance that is being restored from the backup"),
    )
    flavor = models.ForeignKey(
        Flavor,
        related_name="+",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_(
            "Flavor to be used for the restored instance. If not specified, original instance flavor will be used"
        ),
    )

    class Permissions:
        customer_path = "backup__project__customer"
        project_path = "backup__project"


class NetworkRBACPolicy(
    core_models.UuidMixin, core_models.BackendMixin, core_models.TimeStampedModel
):
    class Permissions:
        customer_path = "network__tenant__project__customer"
        project_path = "network__tenant__project"

    class NetworkShareType:
        SHARED = "access_as_shared"
        EXTERNAL = "access_as_external"

        CHOICES = (
            (SHARED, "Shared"),
            (EXTERNAL, "External"),
        )

    network = models.ForeignKey(
        Network,
        on_delete=models.CASCADE,
        related_name="rbac_policies",
        help_text=_("Network that this RBAC policy applies to"),
    )

    target_tenant = models.ForeignKey["Tenant"](
        "openstack.Tenant",
        on_delete=models.CASCADE,
        related_name="network_rbac_policies",
        help_text=_("Tenant that is granted access to the network through this policy"),
    )

    policy_type = models.CharField(
        max_length=255,
        default=NetworkShareType.SHARED,
        choices=NetworkShareType.CHOICES,
        help_text=_(
            "Type of access granted - either shared access or external network access"
        ),
    )

    class Meta:
        verbose_name = "Network RBAC Policy"
        verbose_name_plural = "Network RBAC Policies"
        unique_together = ("network", "target_tenant", "policy_type")
        ordering = ["-created", "id"]

    def __str__(self):
        return f"RBAC policy for {self.network} to {self.target_tenant}"
