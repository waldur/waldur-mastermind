from urllib.parse import urlparse

from django.core import validators
from django.db import models
from django.db.models import Q
from django.template.defaultfilters import slugify
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.fields import JSONField
from waldur_core.quotas.fields import QuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import filter_queryset_for_user


def build_tenants_query(user):
    tenants = filter_queryset_for_user(Tenant.objects.all(), user)
    settings = filter_queryset_for_user(
        structure_models.ServiceSettings.objects.all(), user
    )
    return Q(Q(tenants=None) | Q(tenants__in=tenants)) & Q(settings__in=settings)


class Flavor(structure_models.ServiceProperty):
    cores = models.PositiveSmallIntegerField(help_text=_("Number of cores in a VM"))
    ram = models.PositiveIntegerField(help_text=_("Memory size in MiB"))
    disk = models.PositiveIntegerField(help_text=_("Root disk size in MiB"))
    tenants = models.ManyToManyField(to="Tenant", related_name="flavors")

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
    tenants = models.ManyToManyField(to="Tenant", related_name="images")

    class Permissions:
        build_query = build_tenants_query

    @classmethod
    def get_backend_fields(cls):
        return super().get_backend_fields() + ("min_disk", "min_ram")

    @classmethod
    def get_url_name(cls):
        return "openstack-image"


class VolumeType(core_models.DescribableMixin, structure_models.ServiceProperty):
    tenants = models.ManyToManyField(to="Tenant", related_name="volume_types")

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
        on_delete=models.CASCADE, to="Tenant", related_name="server_groups"
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
        on_delete=models.CASCADE, to="Tenant", related_name="security_groups"
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
        on_delete=models.CASCADE, to="Tenant", related_name="floating_ips"
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
        return self.service_settings.get_backend(tenant_id=self.backend_id)

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
        to="openstack_tenant.Instance",
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
