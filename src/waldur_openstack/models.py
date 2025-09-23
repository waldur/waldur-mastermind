import logging
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from django.core import validators
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.template.defaultfilters import slugify
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import exceptions as core_exceptions
from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.fields import JSONField
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


def build_tenants_query(user):
    return Q(tenants__in=filter_queryset_for_user(Tenant.objects.all(), user))


class Tenant(
    core_models.ActionMixin,
    quotas_models.QuotaModelMixin,
    core_models.RuntimeStateMixin,
    structure_models.BaseResource,
):
    flavors: models.Manager["Flavor"]
    images: models.Manager["Image"]
    volume_types: models.Manager["VolumeType"]
    server_groups: models.Manager["ServerGroup"]
    security_groups: models.Manager["SecurityGroup"]
    floating_ips: models.Manager["FloatingIP"]
    routers: models.Manager["Router"]
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

    tracker = cast(FieldInstanceTracker, FieldTracker())

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


class ServerGroup(structure_models.BaseResource):
    AFFINITY = "affinity"

    POLICIES = ((AFFINITY, "Affinity"),)

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


class BaseSecurityGroupRule(core_models.DescribableMixin, models.Model):
    class Meta:
        abstract = True

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

    protocol = models.CharField(
        max_length=40,
        blank=True,
        choices=PROTOCOLS,
        help_text=_("The network protocol (TCP, UDP, ICMP, or empty for any protocol)"),
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

    tracker = cast(FieldInstanceTracker, FieldTracker())

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return "openstack-router"


class Network(core_models.RuntimeStateMixin, structure_models.BaseResource):
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


class Volume(core_models.ActionMixin, TenantQuotaMixin, structure_models.Storage):
    snapshots: models.Manager["Snapshot"]
    restoration: models.Manager["SnapshotRestoration"]

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
        max_length=150,
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

    @property
    def extend_enabled(self):
        from waldur_openstack import utils

        try:
            utils.check_volume_resize_enabled(self)
            return True
        except core_exceptions.IncorrectStateException:
            return False


class Snapshot(core_models.ActionMixin, TenantQuotaMixin, structure_models.Storage):
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
    core_models.ActionMixin, TenantQuotaMixin, structure_models.VirtualMachine
):
    tracker = cast(FieldInstanceTracker, FieldTracker())

    id: int
    ports: "RelatedManager[Port]"
    volumes: "RelatedManager[Volume]"
    backups: "RelatedManager[Backup]"

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
    directly_connected_ips = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Comma-separated list of directly connected IP addresses"),
    )
    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Meta:
        unique_together = ("service_settings", "backend_id")
        ordering = ["name", "created"]

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
        return set(self.floating_ips.values_list("external_address", flat=True))

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
        ordering = ["-created"]

    def __str__(self):
        return f"RBAC policy for {self.network} to {self.target_tenant}"
