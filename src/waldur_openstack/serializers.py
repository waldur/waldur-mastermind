import collections
import copy
import logging
import re
from ipaddress import (
    AddressValueError,
    IPv4Network,
    NetmaskValueError,
    ip_address,
    ip_network,
)
from typing import cast

from django.conf import settings
from django.contrib.auth import password_validation
from django.core.exceptions import (
    ValidationError,
)
from django.core.validators import validate_ipv46_address
from django.db import transaction
from django.db.models import Q
from django.template.defaultfilters import slugify
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from netaddr import AddrFormatError, IPNetwork, all_matching_cidrs
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_core.core.validators import (
    BackendURLValidator,
    is_valid_ipv4_cidr,
    is_valid_ipv6_cidr,
    validate_x509_certificate,
)
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.quotas.models import SharedQuotaMixin
from waldur_core.quotas.serializers import QuotaSerializer
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_openstack.utils import (
    get_valid_availability_zones,
    is_flavor_valid_for_tenant,
    is_image_valid_for_tenant,
    is_valid_volume_type_name,
    is_volume_type_valid_for_tenant,
    volume_type_name_to_quota_name,
)

from . import models

logger = logging.getLogger(__name__)

FloatingIPSpec = list[tuple[models.FloatingIP | None, models.SubNet]]


class OpenStackServiceSerializer(structure_serializers.ServiceOptionsSerializer):
    class Meta:
        secret_fields = ("backend_url", "username", "password", "domain", "certificate")

    certificate = serializers.CharField(
        required=False, validators=[validate_x509_certificate]
    )

    backend_url = serializers.CharField(
        max_length=200,
        label=_("API URL"),
        default="http://keystone.example.com:5000/v3",
        help_text=_("Keystone auth URL (e.g. http://keystone.example.com:5000/v3)"),
        validators=[BackendURLValidator],
    )

    username = serializers.CharField(
        max_length=100, help_text=_("Administrative user"), default="admin"
    )

    password = serializers.CharField(max_length=100)

    domain = serializers.CharField(
        max_length=200,
        help_text=_("Domain name. If not defined default domain will be used."),
        required=False,
        allow_null=True,
    )

    auth_type = serializers.ChoiceField(
        source="options.auth_type",
        choices=[
            ("password", "Password"),
            ("v3applicationcredential", "Application Credential"),
        ],
        default="password",
        required=False,
        help_text=_("Authentication method: password or v3applicationcredential"),
    )

    availability_zone = serializers.CharField(
        source="options.availability_zone",
        help_text=_("Default availability zone for provisioned instances"),
        required=False,
    )

    flavor_exclude_regex = serializers.CharField(
        source="options.flavor_exclude_regex",
        help_text=_(
            "Flavors matching this regex expression will not be pulled from the backend."
        ),
        required=False,
    )

    volume_type_blacklist = serializers.CharField(
        source="options.volume_type_blacklist",
        help_text=_(
            "List of coma-separated volume types which should not be possible to select when creating VM/Volume."
        ),
        required=False,
    )

    live_resize_of_volumes_enabled = serializers.BooleanField(
        source="options.live_resize_of_volumes_enabled",
        default=False,
        required=False,
    )

    console_type = serializers.CharField(
        source="options.console_type",
        help_text=_(
            "The type of remote console. "
            "The valid values are novnc, xvpvnc, rdp-html5, "
            "spice-html5, serial, and webmks."
        ),
        default="novnc",
        required=False,
    )

    config_drive = serializers.BooleanField(
        source="options.config_drive",
        help_text=_("Indicates whether a config drive enables metadata injection"),
        required=False,
    )

    verify_ssl = serializers.BooleanField(
        source="options.verify_ssl",
        help_text=_("Verify or ignore server certificate"),
        default=False,
        required=False,
    )

    tenant_name = serializers.CharField(
        source="options.tenant_name",
        label=_("Tenant name"),
        default="admin",
        required=False,
    )

    volume_availability_zone_name = serializers.CharField(
        source="options.volume_availability_zone_name",
        label=_("Name of default volume availability zone to use"),
        help_text=_("Default availability zone name for provisioned volumes"),
        required=False,
    )

    valid_availability_zones = serializers.CharField(
        source="options.valid_availability_zones",
        help_text=_(
            "Optional dictionary where key is Nova availability "
            "zone name and value is Cinder availability zone name."
        ),
        required=False,
    )

    external_network_id = serializers.CharField(
        source="options.external_network_id",
        help_text=_(
            "ID of OpenStack external network that will be connected to tenants"
        ),
        label=_("Public/gateway network UUID"),
        required=False,
    )

    latitude = serializers.CharField(
        source="options.latitude",
        help_text=_("Latitude of the datacenter (e.g. 40.712784)"),
        required=False,
    )

    longitude = serializers.CharField(
        source="options.longitude",
        help_text=_("Longitude of the datacenter (e.g. -74.005941)"),
        required=False,
    )

    access_url = serializers.CharField(
        source="options.access_url",
        label=_("Access URL"),
        help_text=_("Publicly accessible OpenStack dashboard URL"),
        required=False,
    )

    console_domain_override = serializers.CharField(
        source="options.console_domain_override",
        label=_("Console domain override"),
        help_text=_(
            "Override of the console URL domain. Supports hostname (e.g. lb.example.com) or hostname:port (e.g. lb.example.com:443)."
        ),
        required=False,
    )

    dns_nameservers = serializers.ListField(
        child=serializers.CharField(),
        source="options.dns_nameservers",
        help_text=_(
            "Default value for new subnets DNS name servers. Should be defined as list."
        ),
        required=False,
    )

    create_ha_routers = serializers.BooleanField(
        source="options.create_ha_routers",
        default=False,
        help_text=_("Create highly available Neutron routers."),
        required=False,
    )

    max_concurrent_provision_instance = serializers.IntegerField(
        source="options.max_concurrent_provision_instance",
        help_text=_(
            "Maximum parallel executions of provisioning operations for instances."
        ),
        required=False,
    )

    max_concurrent_provision_volume = serializers.IntegerField(
        source="options.max_concurrent_provision_volume",
        help_text=_(
            "Maximum parallel executions of provisioning operations for volumes."
        ),
        required=False,
    )

    max_concurrent_provision_snapshot = serializers.IntegerField(
        source="options.max_concurrent_provision_snapshot",
        help_text=_(
            "Maximum parallel executions of provisioning operations for snapshots."
        ),
        required=False,
    )


class OpenStackFlavorSerializer(
    core_serializers.RestrictedSerializerMixin,
    structure_serializers.BasePropertySerializer,
):
    display_name = serializers.SerializerMethodField()

    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Flavor
        fields = (
            "url",
            "uuid",
            "name",
            "settings",
            "cores",
            "ram",
            "disk",
            "backend_id",
            "display_name",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "settings": {"lookup_field": "uuid"},
        }

    def get_display_name(self, flavor: models.Flavor) -> str:
        return f"{flavor.name} ({flavor.cores} CPU, {flavor.ram} MB RAM, {flavor.disk} MB HDD)"


class OpenStackImageSerializer(structure_serializers.BasePropertySerializer):
    is_rescue_image = serializers.ReadOnlyField()

    class Meta:
        model = models.Image
        fields = (
            "url",
            "uuid",
            "name",
            "min_disk",
            "min_ram",
            "settings",
            "backend_id",
            "backend_created_at",
            "hw_rescue_device",
            "hw_rescue_bus",
            "is_rescue_image",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "settings": {"lookup_field": "uuid"},
        }


class OpenStackVolumeTypeSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.VolumeType
        fields = ("url", "uuid", "name", "description", "settings")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "settings": {
                "lookup_field": "uuid",
                "view_name": "servicesettings-detail",
            },
        }


class ExternalSubnetSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.ExternalSubnet
        fields = (
            "uuid",
            "name",
            "backend_id",
            "cidr",
            "gateway_ip",
            "ip_version",
            "enable_dhcp",
            "allocation_pools",
            "dns_nameservers",
            "public_ip_range",
            "description",
        )


class ExternalNetworkSerializer(
    core_serializers.RestrictedSerializerMixin,
    structure_serializers.BasePropertySerializer,
):
    subnets = ExternalSubnetSerializer(many=True, read_only=True)

    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.ExternalNetwork
        fields = (
            "url",
            "uuid",
            "name",
            "settings",
            "backend_id",
            "is_shared",
            "is_default",
            "status",
            "description",
            "subnets",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "settings": {"lookup_field": "uuid"},
        }


class HypervisorSummarySerializer(serializers.Serializer):
    total_vcpus = serializers.IntegerField()
    used_vcpus = serializers.IntegerField()
    total_memory_mb = serializers.IntegerField()
    used_memory_mb = serializers.IntegerField()
    total_local_gb = serializers.IntegerField()
    used_local_gb = serializers.IntegerField()
    total_running_vms = serializers.IntegerField()


class AllocationCandidatesQuerySerializer(serializers.Serializer):
    """Query params for the Placement allocation-candidates endpoint."""

    settings_uuid = serializers.UUIDField(
        help_text="UUID of the OpenStack ServiceSettings to query."
    )
    resources = serializers.CharField(
        help_text=(
            "Comma-separated resource:amount pairs, e.g. "
            "'VCPU:4,MEMORY_MB:8192,DISK_GB:10'."
        ),
    )
    required = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "Optional comma-separated list of required traits, e.g. "
            "'HW_CPU_X86_AVX2,STORAGE_DISK_SSD'."
        ),
    )
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=100, default=10
    )

    @staticmethod
    def parse_resources(spec: str) -> dict:
        """Parse 'VCPU:4,MEMORY_MB:8192' → {'VCPU': 4, 'MEMORY_MB': 8192}."""
        result = {}
        for pair in (s.strip() for s in spec.split(",") if s.strip()):
            if ":" not in pair:
                raise serializers.ValidationError(
                    f"resources entry '{pair}' must be of form CLASS:N"
                )
            cls, raw = pair.split(":", 1)
            try:
                result[cls.strip().upper()] = int(raw)
            except ValueError as e:
                raise serializers.ValidationError(
                    f"resources entry '{pair}' has non-integer amount"
                ) from e
        if not result:
            raise serializers.ValidationError(
                "resources must contain at least one CLASS:N pair"
            )
        return result


class ResourceClassSummarySerializer(serializers.Serializer):
    used = serializers.IntegerField()
    capacity = serializers.IntegerField()


class ProviderSummarySerializer(serializers.Serializer):
    resources = serializers.DictField(child=ResourceClassSummarySerializer())
    traits = serializers.ListField(child=serializers.CharField())


class AllocationCandidatesResponseSerializer(serializers.Serializer):
    """Response shape for the allocation-candidates endpoint."""

    candidate_count = serializers.IntegerField(
        help_text="Total number of allocation candidates Placement returned."
    )
    provider_summaries = serializers.DictField(
        child=ProviderSummarySerializer(),
        help_text=(
            "Placement's per-provider summary: maps resource_provider_uuid → "
            "{resources: {CLASS: {used, capacity}, ...}, traits: [...]}."
        ),
    )


class InstancePlacementAllocationSerializer(serializers.Serializer):
    """One Placement allocation record for an instance, scoped to a single
    resource provider. Returned as a list (one entry per RP the instance
    consumes from). Audience is restricted at the view layer
    (``can_diagnose_openstack_instance``) — staff, support and service-
    provider owners only — so all fields including the resource provider
    UUID and name are unconditionally exposed here.
    """

    resource_provider_uuid = serializers.CharField()
    resource_provider_name = serializers.CharField()
    resources = serializers.DictField(child=serializers.IntegerField())


class HypervisorSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.Hypervisor
        fields = (
            "url",
            "uuid",
            "name",
            "settings",
            "backend_id",
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
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "settings": {"lookup_field": "uuid"},
        }


class HypervisorInventorySerializer(serializers.HyperlinkedModelSerializer):
    hypervisor = serializers.HyperlinkedRelatedField(
        view_name="openstack-hypervisor-detail",
        lookup_field="uuid",
        read_only=True,
    )
    hypervisor_uuid = serializers.ReadOnlyField(source="hypervisor.uuid")
    hypervisor_name = serializers.ReadOnlyField(source="hypervisor.name")
    settings = serializers.HyperlinkedRelatedField(
        source="hypervisor.settings",
        view_name="servicesettings-detail",
        lookup_field="uuid",
        read_only=True,
    )
    settings_uuid = serializers.ReadOnlyField(source="hypervisor.settings.uuid")
    effective_total = serializers.ReadOnlyField()

    class Meta:
        model = models.HypervisorInventory
        fields = (
            "url",
            "uuid",
            "hypervisor",
            "hypervisor_uuid",
            "hypervisor_name",
            "settings",
            "settings_uuid",
            "resource_class",
            "total",
            "reserved",
            "allocation_ratio",
            "used",
            "effective_total",
        )
        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "openstack-hypervisor-inventory-detail",
            },
        }


class OpenStackTenantQuotaSerializer(serializers.Serializer):
    instances = serializers.IntegerField(min_value=1, required=False)
    volumes = serializers.IntegerField(min_value=1, required=False)
    snapshots = serializers.IntegerField(min_value=1, required=False)
    ram = serializers.IntegerField(min_value=1, required=False)
    vcpu = serializers.IntegerField(min_value=1, required=False)
    storage = serializers.IntegerField(min_value=1, required=False)
    security_group_count = serializers.IntegerField(min_value=1, required=False)
    security_group_rule_count = serializers.IntegerField(min_value=1, required=False)
    # Neutron quotas: 0 means "deny all", -1 means "unlimited"
    floating_ip_count = serializers.IntegerField(min_value=-1, required=False)
    network_count = serializers.IntegerField(min_value=-1, required=False)
    subnet_count = serializers.IntegerField(min_value=-1, required=False)
    port_count = serializers.IntegerField(min_value=-1, required=False)

    def to_internal_value(self, data):
        # Accept declared fields via default path.
        result = super().to_internal_value(data)

        # Accept dynamic per-volume-type storage quota keys of the form
        # gigabytes_<volume_type_name> (e.g. gigabytes_ssd, gigabytes___DEFAULT__).
        # Cinder stores and returns these in GB (not MiB), so values are passed
        # through unchanged; no unit conversion is applied here or in push_tenant_quotas.
        errors = {}
        for key, value in data.items():
            if not is_valid_volume_type_name(key):
                continue
            if key in result:
                # Already handled by a declared field — should not happen, but guard anyway.
                continue
            try:
                coerced = int(value)
            except (TypeError, ValueError):
                errors[key] = [_("A valid integer is required.")]
                continue
            if coerced < -1:
                errors[key] = [_("Ensure this value is greater than or equal to -1.")]
                continue
            result[key] = coerced

        if errors:
            raise serializers.ValidationError(errors)

        return result


class OpenStackFixedIpSerializer(serializers.Serializer):
    ip_address = serializers.IPAddressField(
        help_text=_("IP address to assign to the port")
    )
    subnet_id = serializers.CharField(
        help_text=_("ID of the subnet in which to assign the IP address")
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        subnet_id = attrs.get("subnet_id")
        port_ip = attrs.get("ip_address")

        try:
            subnet = models.SubNet.objects.get(backend_id=subnet_id)
        except models.SubNet.DoesNotExist:
            raise serializers.ValidationError(_("Subnet with this ID does not exist."))

        ip_addr = ip_address(port_ip)

        if subnet.allocation_pools:
            in_allocation_pool = False
            for pool in subnet.allocation_pools:
                start_ip = ip_address(pool["start"])
                end_ip = ip_address(pool["end"])
                if start_ip <= ip_addr <= end_ip:
                    in_allocation_pool = True
                    break

            if not in_allocation_pool:
                logger.info(
                    "Requested IP address %s is outside the allocation pools.",
                    ip_address,
                )

        return attrs


@extend_schema_field(OpenStackFixedIpSerializer(many=True))
class OpenStackFixedIpField(serializers.JSONField):
    pass


class OpenStackFloatingIPSerializer(structure_serializers.BaseResourceActionSerializer):
    port = serializers.HyperlinkedRelatedField(
        view_name="openstack-port-detail",
        lookup_field="uuid",
        read_only=True,
    )

    port_fixed_ips = OpenStackFixedIpField(source="port.fixed_ips", read_only=True)
    router = serializers.HyperlinkedRelatedField(
        view_name="openstack-router-detail",
        lookup_field="uuid",
        queryset=models.Router.objects.all(),
        required=False,
        allow_null=True,
        write_only=True,
        help_text=_("Optional router to use for external network detection"),
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.FloatingIP
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "runtime_state",
            "address",
            "backend_network_id",
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "port",
            "external_address",
            "port_fixed_ips",
            "router",
        )
        related_paths = ("tenant",)
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "runtime_state",
                "address",
                "description",
                "name",
                "tenant",
                "backend_network_id",
                "service_settings",
                "project",
                "port",
            )
        )
        extra_kwargs = dict(
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs,
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs["tenant"] = tenant = self.context["view"].get_object()
        attrs["service_settings"] = tenant.service_settings
        attrs["project"] = tenant.project

        router = attrs.get("router")
        if router and router.tenant != tenant:
            raise serializers.ValidationError(
                {"router": _("Router must belong to the same tenant.")}
            )

        return super().validate(attrs)


class OpenStackFloatingIPAttachSerializer(serializers.Serializer):
    port = serializers.HyperlinkedRelatedField(
        queryset=models.Port.objects.all(),
        view_name="openstack-port-detail",
        lookup_field="uuid",
        many=False,
        required=True,
    )


class OpenStackFloatingIPDescriptionUpdateSerializer(serializers.Serializer):
    description = serializers.CharField(
        required=False, help_text=_("New floating IP description.")
    )


class BaseSecurityGroupRuleSerializer(serializers.ModelSerializer):
    remote_group_name = serializers.ReadOnlyField(source="remote_group.name")
    remote_group_uuid = serializers.UUIDField(
        read_only=True, source="remote_group.uuid"
    )

    class Meta:
        fields = (
            "ethertype",
            "direction",
            "protocol",
            "from_port",
            "to_port",
            "cidr",
            "description",
            "remote_group_name",
            "remote_group_uuid",
        )


class DebugSecurityGroupRuleSerializer(BaseSecurityGroupRuleSerializer):
    class Meta(BaseSecurityGroupRuleSerializer.Meta):
        model = models.SecurityGroupRule


def validate_security_group_rule(rule: dict):
    ethertype = rule.get("ethertype", models.SecurityGroupRule.IPv4)
    protocol = rule.get("protocol")
    from_port = rule.get("from_port")
    to_port = rule.get("to_port")
    cidr = rule.get("cidr")
    # for managed rancher remote group is not used
    remote_group = rule.get("remote_group")

    if cidr:
        if ethertype == models.SecurityGroupRule.IPv4 and not is_valid_ipv4_cidr(cidr):
            raise serializers.ValidationError(
                {
                    "cidr": _(
                        "Expected CIDR format: <0-255>.<0-255>.<0-255>.<0-255>/<0-32>"
                    )
                }
            )
        elif ethertype == models.SecurityGroupRule.IPv6 and not is_valid_ipv6_cidr(
            cidr
        ):
            raise serializers.ValidationError(
                {
                    "cidr": _(
                        "IPv6 addresses are represented as eight groups, separated by colons."
                    )
                }
            )

    if cidr and remote_group:
        raise serializers.ValidationError(
            _("You can specify either the remote_group_id or cidr attribute, not both.")
        )

    if to_port is None:
        raise serializers.ValidationError({"to_port": _("Empty value is not allowed.")})

    if from_port is None:
        raise serializers.ValidationError(
            {"from_port": _("Empty value is not allowed.")}
        )

    if protocol == "icmp":
        if from_port is not None and not -1 <= from_port <= 255:
            raise serializers.ValidationError(
                {
                    "from_port": _("Value should be in range [-1, 255], found %d")
                    % from_port
                }
            )
        if to_port is not None and not -1 <= to_port <= 255:
            raise serializers.ValidationError(
                {"to_port": _("Value should be in range [-1, 255], found %d") % to_port}
            )

    elif protocol in ("tcp", "udp"):
        if from_port is not None and to_port is not None:
            if from_port > to_port:
                raise serializers.ValidationError(
                    _('"from_port" should be less or equal to "to_port"')
                )
        if from_port == -1 and to_port != -1:
            raise serializers.ValidationError(
                _('"from_port" should not be -1 if "to_port" is defined.')
            )
        if from_port is not None and from_port != -1 and from_port < 1:
            raise serializers.ValidationError(
                {
                    "from_port": _("Value should be in range [1, 65535], found %d")
                    % from_port
                }
            )
        if to_port is not None and to_port != -1 and to_port < 1:
            raise serializers.ValidationError(
                {
                    "to_port": _("Value should be in range [1, 65535], found %d")
                    % to_port
                }
            )

    elif protocol == "":
        # See also: https://github.com/openstack/neutron/blob/af130e79cbe5d12b7c9f9f4dcbcdc8d972bfcfd4/neutron/db/securitygroups_db.py#L500

        if from_port != -1:
            raise serializers.ValidationError(
                {
                    "from_port": _(
                        "Port range is not supported if protocol is not specified."
                    )
                }
            )

        if to_port != -1:
            raise serializers.ValidationError(
                {
                    "to_port": _(
                        "Port range is not supported if protocol is not specified."
                    )
                }
            )

    else:
        raise serializers.ValidationError(
            {
                "protocol": _("Value should be one of (tcp, udp, icmp), found %s")
                % protocol
            }
        )


class OpenStackSecurityGroupRuleSerializer(
    BaseSecurityGroupRuleSerializer, serializers.HyperlinkedModelSerializer
):
    class Meta(BaseSecurityGroupRuleSerializer.Meta):
        model = models.SecurityGroupRule
        fields = BaseSecurityGroupRuleSerializer.Meta.fields + ("id", "remote_group")
        extra_kwargs = dict(
            remote_group={"lookup_field": "uuid", "view_name": "openstack-sgp-detail"},
        )

    def validate(self, rule):
        validate_security_group_rule(self.to_representation(rule))
        return rule


class OpenStackSecurityGroupRuleCreateSerializer(OpenStackSecurityGroupRuleSerializer):
    """Create rules on security group creation"""

    def to_internal_value(self, data):
        if "id" in data:
            raise serializers.ValidationError(
                _("Cannot add existed rule with id %s to new security group")
                % data["id"]
            )
        internal_data = super(
            OpenStackSecurityGroupRuleSerializer, self
        ).to_internal_value(data)
        return models.SecurityGroupRule(**internal_data)


class OpenStackSecurityGroupRuleUpdateSerializer(OpenStackSecurityGroupRuleSerializer):
    class Meta(OpenStackSecurityGroupRuleSerializer.Meta):
        extra_kwargs = {
            **OpenStackSecurityGroupRuleSerializer.Meta.extra_kwargs,
            "direction": {"default": models.BaseSecurityGroupRule.INGRESS},
            "ethertype": {"default": models.BaseSecurityGroupRule.IPv4},
        }

    def to_internal_value(self, data):
        """Create new rule if id is not specified, update exist rule if id is specified"""
        security_group = self.context["view"].get_object()
        internal_data = super(
            OpenStackSecurityGroupRuleSerializer, self
        ).to_internal_value(data)
        if "id" not in data:
            return models.SecurityGroupRule(
                security_group=security_group, **internal_data
            )
        rule_id = data.pop("id")
        try:
            rule = security_group.rules.select_related("remote_group").get(id=rule_id)
        except models.SecurityGroupRule.DoesNotExist:
            raise serializers.ValidationError(
                {"id": _("Security group does not have rule with id %s.") % rule_id}
            )
        for key, value in internal_data.items():
            setattr(rule, key, value)
        return rule


def validate_duplicate_security_group_rules(rules):
    values = rules.values_list(
        "ethertype",
        "direction",
        "protocol",
        "from_port",
        "to_port",
        "cidr",
        "remote_group",
    )
    if len(set(values)) != len(values):
        raise serializers.ValidationError(
            _("Duplicate security group rules are not allowed.")
        )


class OpenStackSecurityGroupRuleListUpdateSerializer(serializers.ListSerializer):
    child = OpenStackSecurityGroupRuleUpdateSerializer()

    @transaction.atomic()
    def save(self, **kwargs):
        security_group = self.context["view"].get_object()
        old_rules_count = security_group.rules.count()
        rules = self.validated_data
        security_group.rules.exclude(id__in=[r.id for r in rules if r.id]).delete()
        for rule in rules:
            rule.save()
        validate_duplicate_security_group_rules(security_group.rules)
        security_group.change_backend_quotas_usage_on_rules_update(
            old_rules_count, validate=True
        )
        return rules


class OpenStackSecurityGroupSerializer(
    structure_serializers.BaseResourceActionSerializer
):
    rules = OpenStackSecurityGroupRuleCreateSerializer(many=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.SecurityGroup
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "rules",
        )
        related_paths = ("tenant",)
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + ("service_settings", "project")
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "openstack-sgp-detail"},
            "tenant": {
                "lookup_field": "uuid",
                "view_name": "openstack-tenant-detail",
                "read_only": True,
            },
        }

    def validate_rules(self, value):
        for rule in value:
            if rule.id is not None:
                raise serializers.ValidationError(
                    _("Cannot add existing rule with id %s to new security group")
                    % rule.id
                )
            rule.full_clean(exclude=["security_group"])
        return value

    def validate_name(self, value):
        if value == "default":
            raise serializers.ValidationError(
                _("Default security group is managed by OpenStack itself.")
            )
        return value

    def validate(self, attrs):
        tenant = self.context["view"].get_object()
        name = attrs["name"]

        if tenant.security_groups.filter(name=name):
            raise serializers.ValidationError(
                _("Security group name should be unique.")
            )

        attrs["tenant"] = tenant
        attrs["service_settings"] = tenant.service_settings
        attrs["project"] = tenant.project
        return super().validate(attrs)

    def create(self, validated_data):
        rules = validated_data.pop("rules", [])
        with transaction.atomic():
            # quota usage has to be increased only after rules creation,
            # so we cannot execute BaseResourceSerializer create method.
            security_group = super(
                structure_serializers.BaseResourceSerializer, self
            ).create(validated_data)
            for rule in rules:
                security_group.rules.add(rule, bulk=False)
            validate_duplicate_security_group_rules(security_group.rules)
            security_group.increase_backend_quotas_usage(validate=True)
        return security_group


class OpenStackSecurityGroupUpdateSerializer(serializers.ModelSerializer):
    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.SecurityGroup
        fields = ("name", "description")

    def validate_name(self, name):
        if name:
            if name == "default":
                raise serializers.ValidationError(
                    _("Default security group is managed by OpenStack itself.")
                )

            if self.instance.tenant.security_groups.filter(name=name).exclude(
                pk=self.instance.pk
            ):
                raise serializers.ValidationError(
                    _("Security group name should be unique.")
                )
        return name


class OpenStackSecurityGroupRuleUpdateByNameSerializer(
    OpenStackSecurityGroupRuleSerializer
):
    remote_group = serializers.HyperlinkedRelatedField(
        lookup_field="uuid",
        view_name="openstack-sgp-detail",
        required=False,
        queryset=models.SecurityGroup.objects.all(),
    )
    remote_group_name = serializers.CharField(write_only=True, required=False)

    class Meta(OpenStackSecurityGroupRuleSerializer.Meta):
        fields = OpenStackSecurityGroupRuleSerializer.Meta.fields + (
            "remote_group_name",
        )


class TenantSecurityGroupUpdateSerializer(serializers.ModelSerializer):
    uuid = serializers.UUIDField(required=False)
    rules = OpenStackSecurityGroupRuleUpdateByNameSerializer(many=True, required=False)

    class Meta:
        model = models.SecurityGroup
        fields = ("uuid", "name", "description", "rules")

    def validate_name(self, value):
        if value == "default":
            raise serializers.ValidationError(
                _("Default security group is managed by OpenStack itself.")
            )
        return value


class TenantPushSecurityGroupsSerializer(serializers.ListSerializer):
    child = TenantSecurityGroupUpdateSerializer()

    def validate(self, data):
        names = [sg["name"] for sg in data]
        if len(names) != len(set(names)):
            raise serializers.ValidationError(
                {"security_groups": _("Security group names must be unique.")}
            )

        tenant = cast(models.Tenant, self.instance)
        for sg_data in data:
            if "uuid" in sg_data:
                if not tenant.security_groups.filter(uuid=sg_data["uuid"]).exists():
                    raise serializers.ValidationError(
                        _("Security group with UUID %s does not belong to the tenant.")
                        % sg_data["uuid"]
                    )
            if tenant.security_groups.filter(name=sg_data["name"]).exclude(
                uuid=sg_data.get("uuid")
            ):
                raise serializers.ValidationError(
                    {"name": _("Security group with this name already exists.")}
                )

        return data

    @transaction.atomic
    def save(self, **kwargs):
        tenant = cast(models.Tenant, self.instance)
        validated_data = self.validated_data

        # Maps for quick lookups
        existing_sgs_by_uuid = {sg.uuid.hex: sg for sg in tenant.security_groups.all()}
        sg_payload_by_uuid = {d["uuid"].hex: d for d in validated_data if "uuid" in d}

        # 1. Delete SGs from DB not in payload
        uuids_to_delete = set(existing_sgs_by_uuid.keys()) - set(
            sg_payload_by_uuid.keys()
        )
        tenant.security_groups.filter(uuid__in=uuids_to_delete).delete()

        # 2. First pass: Create/update SGs to ensure they all exist in DB before processing rules
        sgs_in_payload_by_name: dict[str, models.SecurityGroup] = {}
        for sg_data in validated_data:
            sg_uuid = sg_data.get("uuid")
            if sg_uuid:
                sg = existing_sgs_by_uuid[sg_uuid.hex]
                sg.name = sg_data["name"]
                sg.description = sg_data.get("description", "")
                sg.save()
            else:
                sg = models.SecurityGroup.objects.create(
                    tenant=tenant,
                    project=tenant.project,
                    service_settings=tenant.service_settings,
                    name=sg_data["name"],
                    description=sg_data.get("description", ""),
                )
            sgs_in_payload_by_name[sg.name] = sg

        # 3. Second pass: update rules now that all groups exist
        for sg_data in validated_data:
            sg = sgs_in_payload_by_name[sg_data["name"]]
            rules_data = sg_data.get("rules", [])

            sg.rules.all().delete()
            for rule_data in rules_data:
                remote_group_name = rule_data.pop("remote_group_name", None)
                if remote_group_name:
                    if remote_group_name in sgs_in_payload_by_name:
                        rule_data["remote_group"] = sgs_in_payload_by_name[
                            remote_group_name
                        ]
                    else:
                        try:
                            remote_sg = tenant.security_groups.get(
                                name=remote_group_name
                            )
                            rule_data["remote_group"] = remote_sg
                        except models.SecurityGroup.DoesNotExist:
                            pass  # Let it fail on DB level if remote group is not found

                models.SecurityGroupRule.objects.create(security_group=sg, **rule_data)

        return tenant.security_groups.all()


class OpenStackNestedInstanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Instance
        fields = ("backend_id", "name", "uuid")


class OpenStackServerGroupSerializer(
    structure_serializers.BaseResourceActionSerializer
):
    class Meta:
        model = models.ServerGroup
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "policy",
            "display_name",
            "name",
            "instances",
        )
        related_paths = ("tenant",)
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + ("service_settings", "project")
        )

        extra_kwargs = {
            "url": {
                "lookup_field": "uuid",
                "view_name": "openstack-server-group-detail",
            },
            "tenant": {
                "lookup_field": "uuid",
                "view_name": "openstack-tenant-detail",
                "read_only": True,
            },
            "policy": {
                "help_text": _(
                    "affinity — all instances are placed on the same hypervisor. "
                    "anti-affinity — all instances are placed on different hypervisors. "
                    "soft-affinity — instances are placed on the same hypervisor if possible, but not enforced. "
                    "soft-anti-affinity — instances are placed on different hypervisors if possible, but not enforced."
                ),
            },
        }

    display_name = serializers.SerializerMethodField()
    instances = serializers.SerializerMethodField()

    def get_display_name(self, server_group) -> str:
        return f"Name: {server_group.name}, Policy: {server_group.policy}"

    @extend_schema_field(OpenStackNestedInstanceSerializer(many=True))
    def get_instances(self, server_group):
        filtered_instances = (
            models.Instance.objects.filter(
                server_group__backend_id=server_group.backend_id
            )
            .values("backend_id", "name", "uuid")
            .order_by("name")
        )
        return filtered_instances

    def validate(self, attrs):
        tenant = self.context["view"].get_object()
        name = attrs["name"]

        if tenant.server_groups.filter(name=name):
            raise serializers.ValidationError("Server group name should be unique.")

        attrs["tenant"] = tenant
        attrs["service_settings"] = tenant.service_settings
        attrs["project"] = tenant.project
        return super().validate(attrs)


ALLOWED_PRIVATE_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)


def validate_private_cidr(value, enforced_prefixlen=None):
    try:
        network = IPv4Network(value, strict=True)
    except (AddressValueError, NetmaskValueError, ValueError):
        raise ValidationError(
            message=_("Enter a valid IPv4 address."),
            code="invalid",
        )

    if enforced_prefixlen and network.prefixlen != enforced_prefixlen:
        raise ValidationError(
            message=_("Network mask length should be equal to %s.")
            % enforced_prefixlen,
            code="invalid",
        )

    if not any(network.subnet_of(net) for net in ALLOWED_PRIVATE_NETWORKS):
        raise ValidationError(
            message=_("A private network CIDR is expected."),
            code="invalid",
        )

    return network.with_prefixlen


def validate_private_subnet_cidr(value):
    return validate_private_cidr(value, 24)


class OpenStackTenantSecurityGroupSerializer(serializers.Serializer):
    name = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True)
    rules = OpenStackSecurityGroupRuleCreateSerializer(many=True, required=False)

    def validate_name(self, value):
        if value == "default":
            raise serializers.ValidationError(
                _("Default security group is managed by OpenStack itself.")
            )
        return value


class OpenStackTenantSerializer(structure_serializers.BaseResourceSerializer):
    quotas = QuotaSerializer(many=True, read_only=True)
    subnet_cidr = serializers.CharField(
        default="192.168.42.0/24",
        initial="192.168.42.0/24",
        write_only=True,
    )
    security_groups = OpenStackTenantSecurityGroupSerializer(
        many=True,
        required=False,
        write_only=True,
    )
    skip_creation_of_default_subnet = serializers.BooleanField(
        default=False,
        write_only=True,
    )
    skip_creation_of_default_router = serializers.BooleanField(
        default=False,
    )

    external_network_ref_uuid = serializers.ReadOnlyField(
        source="external_network_ref.uuid",
        default=None,
    )
    external_network_ref_name = serializers.ReadOnlyField(
        source="external_network_ref.name",
        default="",
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Tenant
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "availability_zone",
            "internal_network_id",
            "external_network_id",
            "external_network_ref_uuid",
            "external_network_ref_name",
            "user_username",
            "user_password",
            "quotas",
            "subnet_cidr",
            "default_volume_type_name",
            "security_groups",
            "skip_creation_of_default_subnet",
            "skip_creation_of_default_router",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "internal_network_id",
                "external_network_id",
                "external_network_ref_uuid",
                "external_network_ref_name",
            )
        )
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + (
                "user_username",
                "subnet_cidr",
                "user_password",
            )
        )
        extra_kwargs = dict(
            name={"max_length": 64},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs,
        )

    def get_fields(self):
        fields = super().get_fields()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if not settings.WALDUR_OPENSTACK["TENANT_CREDENTIALS_VISIBLE"]:
            for field in ("user_username", "user_password", "access_url"):
                if field in fields:
                    del fields[field]

        return fields

    def validate_security_groups_configuration(self, attrs):
        security_groups = attrs.get("security_groups")
        if security_groups:
            names = [sg["name"] for sg in security_groups]
            if len(names) != len(set(names)):
                raise serializers.ValidationError(
                    {
                        "security_groups": _(
                            "Security group names must be unique within the request."
                        )
                    }
                )
            return

        plugin_settings = getattr(settings, "WALDUR_OPENSTACK", {})
        config_groups = plugin_settings.get("DEFAULT_SECURITY_GROUPS", [])
        for group in config_groups:
            sg_name = group.get("name")
            if sg_name in (None, ""):
                raise serializers.ValidationError(
                    _(
                        'Skipping misconfigured security group: parameter "name" not found or is empty.'
                    )
                )

            rules = group.get("rules")
            if type(rules) not in (list, tuple):
                raise serializers.ValidationError(
                    _(
                        'Skipping misconfigured security group: parameter "rules" should be list or tuple.'
                    )
                )

    def _get_neighbour_tenants(self, service_settings):
        domain = service_settings.domain
        backend_url = service_settings.backend_url
        tenants = models.Tenant.objects.filter(
            service_settings__backend_url=backend_url
        )
        if domain in (None, "", "default"):
            tenants = tenants.filter(
                Q(service_settings__domain="")
                | Q(service_settings__domain__isnull=True)
                | Q(service_settings__domain__iexact="default")
            )
        else:
            tenants = tenants.filter(service_settings__domain=domain)
        return tenants

    def _validate_tenant_name(self, service_settings, tenant_name):
        neighbour_tenants = self._get_neighbour_tenants(service_settings)
        existing_tenant_names = [
            service_settings.options.get("tenant_name", "admin")
        ] + list(neighbour_tenants.values_list("name", flat=True))
        if tenant_name in existing_tenant_names:
            raise serializers.ValidationError(
                {
                    "name": _(
                        'Name "%s" is already registered. Please choose another one.'
                        % tenant_name
                    ),
                }
            )

    def _validate_username(self, service_settings, username):
        neighbour_tenants = self._get_neighbour_tenants(service_settings)
        existing_usernames = [service_settings.username] + list(
            neighbour_tenants.values_list("user_username", flat=True)
        )
        if username in existing_usernames:
            raise serializers.ValidationError(
                {
                    "user_username": _(
                        'Name "%s" is already registered. Please choose another one.'
                    )
                    % username
                }
            )

        blacklisted_usernames = service_settings.options.get(
            "blacklisted_usernames",
            settings.WALDUR_OPENSTACK["DEFAULT_BLACKLISTED_USERNAMES"],
        )
        if username in blacklisted_usernames:
            raise serializers.ValidationError(
                {
                    "user_username": _(
                        'Name "%s" cannot be used as tenant user username.'
                    )
                    % username
                }
            )

    def validate(self, attrs):
        attrs = super().validate(attrs)

        self.validate_security_groups_configuration(attrs)

        if self.instance is not None:
            service_settings = self.instance.service_settings
        else:
            service_settings = attrs["service_settings"]

        # validate tenant name
        if self.instance is not None and attrs.get("name"):
            if self.instance.name != attrs["name"]:
                self._validate_tenant_name(service_settings, attrs["name"])
        elif attrs.get("name"):
            self._validate_tenant_name(service_settings, attrs["name"])

        # username generation/validation
        if (
            self.instance is not None
            or not settings.WALDUR_OPENSTACK["TENANT_CREDENTIALS_VISIBLE"]
        ):
            return attrs
        else:
            if not attrs.get("user_username"):
                attrs["user_username"] = models.Tenant.generate_username(attrs["name"])

            self._validate_username(service_settings, attrs.get("user_username"))

        return attrs

    def create(self, validated_data):
        service_settings: structure_models.ServiceSettings = validated_data[
            "service_settings"
        ]
        # get availability zone from service settings if it is not defined
        if not validated_data.get("availability_zone"):
            validated_data["availability_zone"] = (
                service_settings.get_option("availability_zone") or ""
            )
        # init tenant user username(if not defined) and password
        slugified_name = slugify(validated_data["name"])[:25]
        if not validated_data.get("user_username"):
            validated_data["user_username"] = models.Tenant.generate_username(
                validated_data["name"]
            )
        validated_data["user_password"] = core_utils.pwgen()

        subnet_cidr = validated_data.pop("subnet_cidr")
        security_groups_data = validated_data.pop("security_groups", None)

        # if MTU was passed
        mtu = validated_data.get("mtu")
        with transaction.atomic():
            tenant: models.Tenant = super().create(validated_data)
            if not validated_data.get("skip_creation_of_default_subnet"):
                network = models.Network.objects.create(
                    name=slugified_name + "-int-net",
                    description=_("Internal network for tenant %s") % tenant.name,
                    tenant=tenant,
                    service_settings=tenant.service_settings,
                    project=tenant.project,
                    mtu=mtu,
                )
                models.SubNet.objects.create(
                    name=slugified_name + "-sub-net",
                    description=_("SubNet for tenant %s internal network")
                    % tenant.name,
                    network=network,
                    tenant=tenant,
                    service_settings=tenant.service_settings,
                    project=tenant.project,
                    cidr=subnet_cidr,
                    dns_nameservers=service_settings.options.get("dns_nameservers", []),
                )
            self.create_default_security_groups(tenant, security_groups_data)

        return tenant

    def create_default_security_groups(
        self, tenant: models.Tenant, security_groups_data: dict | None = None
    ):
        if security_groups_data is not None:
            for group_data in security_groups_data:
                rules: list[models.SecurityGroupRule] = group_data.pop("rules", [])
                sg = models.SecurityGroup.objects.create(
                    service_settings=tenant.service_settings,
                    project=tenant.project,
                    tenant=tenant,
                    **group_data,
                )
                for rule in rules:
                    rule.security_group = sg
                    rule.save()
            return

        plugin_settings = getattr(settings, "WALDUR_OPENSTACK", {})
        config_groups = copy.deepcopy(
            plugin_settings.get("DEFAULT_SECURITY_GROUPS", [])
        )

        for group in config_groups:
            sg_name = group.get("name")
            sg_description = group.get("description", None)
            sg = models.SecurityGroup.objects.get_or_create(
                service_settings=tenant.service_settings,
                project=tenant.project,
                tenant=tenant,
                description=sg_description,
                name=sg_name,
            )[0]

            for rule in group.get("rules"):
                if "icmp_type" in rule:
                    rule["from_port"] = rule.pop("icmp_type")
                if "icmp_code" in rule:
                    rule["to_port"] = rule.pop("icmp_code")

                try:
                    rule = models.SecurityGroupRule(security_group=sg, **rule)
                    rule.full_clean()
                except serializers.ValidationError as e:
                    logger.error(
                        f"Failed to create rule for security group {sg_name}: {e}."
                    )
                else:
                    rule.save()


class OpenStackSubNetAllocationPoolSerializer(serializers.Serializer):
    start = serializers.IPAddressField()
    end = serializers.IPAddressField()


@extend_schema_field(OpenStackSubNetAllocationPoolSerializer(many=True))
class OpenStackSubNetAllocationPoolField(serializers.JSONField):
    pass


class OpenStackNestedSubNetSerializer(serializers.ModelSerializer):
    allocation_pools = OpenStackSubNetAllocationPoolField(read_only=True)

    class Meta:
        model = models.SubNet
        fields = (
            "uuid",
            "name",
            "description",
            "cidr",
            "gateway_ip",
            "allocation_pools",
            "ip_version",
            "enable_dhcp",
        )


class OpenStackStaticRouteSerializer(serializers.Serializer):
    destination = serializers.CharField()
    nexthop = serializers.IPAddressField()

    def validate_destination(self, value):
        try:
            return str(IPNetwork(value))
        except (AddrFormatError, TypeError):
            raise serializers.ValidationError("Invalid CIDR format.")


class OpenStackRouterSetRoutesSerializer(serializers.Serializer):
    routes = OpenStackStaticRouteSerializer(many=True)

    def validate(self, attrs):
        fixed_ips = self.instance.fixed_ips
        for route in attrs["routes"]:
            nexthop = route["nexthop"]
            if nexthop in fixed_ips:
                raise serializers.ValidationError(
                    _("Nexthop %s is used by router.") % nexthop
                )
        return attrs


class SetExternalGatewayFixedIPSerializer(serializers.Serializer):
    ip_address = serializers.CharField(
        help_text=_("IP address specification for the gateway port.")
    )
    subnet_id = serializers.CharField(
        required=False, help_text=_("Backend ID of the subnet.")
    )


class SetExternalGatewaySerializer(serializers.Serializer):
    external_network_id = serializers.CharField(
        help_text=_("Backend ID (OpenStack UUID) of the external network."),
    )
    enable_snat = serializers.BooleanField(
        required=False,
        default=None,
        allow_null=True,
        help_text=_(
            "Whether to enable SNAT on the gateway. "
            "None means use OpenStack default (True). "
            "Requires advanced permissions."
        ),
    )
    external_fixed_ips = SetExternalGatewayFixedIPSerializer(
        many=True,
        required=False,
        default=list,
        help_text=_(
            "List of fixed IP specifications for the gateway port. "
            "Each entry should have 'ip_address' and optionally 'subnet_id'. "
            "Requires advanced permissions."
        ),
    )

    def _resolve_network_source(self, external_network_id, router):
        """Resolve the external network and determine its source type."""
        # Check global ExternalNetwork catalog
        ext_net = models.ExternalNetwork.objects.filter(
            settings=router.tenant.service_settings,
            backend_id=external_network_id,
        ).first()
        if ext_net:
            return "global", ext_net

        # Check RBAC-exposed-as-external networks
        rbac_network = models.Network.objects.filter(
            backend_id=external_network_id,
            rbac_policies__target_tenant=router.tenant,
            rbac_policies__policy_type=models.NetworkRBACPolicy.NetworkShareType.EXTERNAL,
        ).first()
        if rbac_network:
            return "rbac", rbac_network

        return None, None

    def _can_set_advanced_gateway_options(
        self, user, router, network_source, network_obj
    ):
        """Check if user can set enable_snat=False or external_fixed_ips."""
        if user.is_staff:
            return True

        if network_source == "global":
            # Global external networks: provider (service settings owner) only
            service_settings = router.tenant.service_settings
            return service_settings.customer.has_user(user, CustomerRole.OWNER)

        if network_source == "rbac":
            # RBAC-exposed networks: user must have admin/manager on source project
            source_project = network_obj.tenant.project
            return source_project.has_user(
                user, ProjectRole.ADMIN
            ) or source_project.has_user(user, ProjectRole.MANAGER)

        return False

    def validate(self, attrs):
        view = self.context.get("view")
        if not view:
            return attrs

        router = view.get_object()
        request = self.context["request"]
        user = request.user
        external_network_id = attrs["external_network_id"]

        network_source, network_obj = self._resolve_network_source(
            external_network_id, router
        )
        if network_source is None:
            raise serializers.ValidationError(
                {
                    "external_network_id": _(
                        "Network with backend ID '%s' is not available as an external "
                        "network for this router's tenant."
                    )
                    % external_network_id,
                }
            )

        # Check advanced options (SNAT control, fixed IPs)
        enable_snat = attrs.get("enable_snat")
        external_fixed_ips = attrs.get("external_fixed_ips", [])
        needs_advanced = enable_snat is not None or bool(external_fixed_ips)

        if needs_advanced:
            if not self._can_set_advanced_gateway_options(
                user, router, network_source, network_obj
            ):
                raise serializers.ValidationError(
                    _(
                        "You do not have permission to set advanced gateway options "
                        "(SNAT control or fixed IPs) for this network."
                    )
                )

        # Validate external_fixed_ips entries
        for entry in external_fixed_ips:
            if "ip_address" not in entry:
                raise serializers.ValidationError(
                    {
                        "external_fixed_ips": _(
                            "Each entry must contain an 'ip_address' field."
                        )
                    }
                )

        # Store resolved data for the view
        attrs["network_source"] = network_source
        attrs["network_obj"] = network_obj
        if network_source == "global":
            attrs["external_network_ref"] = network_obj
        else:
            attrs["external_network_ref"] = None

        return attrs


class AvailableExternalNetworkSubnetSerializer(serializers.Serializer):
    backend_id = serializers.CharField()
    name = serializers.CharField()
    cidr = serializers.CharField()


class AvailableExternalNetworkSerializer(serializers.Serializer):
    backend_id = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    source = serializers.ChoiceField(choices=["global", "rbac"])
    subnets = AvailableExternalNetworkSubnetSerializer(many=True)


class OpenStackAllowedAddressPairSerializer(serializers.Serializer):
    ip_address = serializers.CharField(
        default="192.168.42.0/24",
        initial="192.168.42.0/24",
        write_only=True,
    )
    mac_address = serializers.CharField(required=False)

    def validate_ip_address(self, value):
        return validate_private_cidr(value)


@extend_schema_field(OpenStackAllowedAddressPairSerializer(many=True))
class OpenStackAllowedAddressPairField(serializers.JSONField):
    pass


class OpenStackPortNestedSecurityGroupSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    class Meta:
        model = models.SecurityGroup
        fields = ("uuid", "name", "url")
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "openstack-sgp-detail"}
        }


class OpenStackPortSerializer(structure_serializers.BaseResourceActionSerializer):
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    tenant_uuid = serializers.UUIDField(source="tenant.uuid", read_only=True)
    network_name = serializers.CharField(source="network.name", read_only=True)
    network_uuid = serializers.UUIDField(source="network.uuid", read_only=True)
    allowed_address_pairs = OpenStackAllowedAddressPairField(required=False)
    floating_ips = serializers.HyperlinkedRelatedField(
        view_name="openstack-fip-detail",
        lookup_field="uuid",
        read_only=True,
        many=True,
    )
    fixed_ips = OpenStackFixedIpField(required=False)
    security_groups = OpenStackPortNestedSecurityGroupSerializer(
        many=True, required=False
    )
    target_tenant = serializers.HyperlinkedRelatedField(
        view_name="openstack-tenant-detail",
        lookup_field="uuid",
        queryset=models.Tenant.objects.filter(state=CoreStates.OK).all(),
        write_only=True,
        required=False,
        help_text="Target tenant for shared network port creation. If not specified, defaults to network's tenant.",
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Port
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "fixed_ips",
            "mac_address",
            "allowed_address_pairs",
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "target_tenant",
            "network",
            "network_name",
            "network_uuid",
            "floating_ips",
            "device_id",
            "device_owner",
            "port_security_enabled",
            "security_groups",
            "admin_state_up",
            "status",
        )
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + (
                "network",
                "port_security_enabled",
                "fixed_ips",
                "mac_address",
                "allowed_address_pairs",
            )
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "tenant",
                "allowed_address_pairs",
                "device_id",
                "device_owner",
                "security_groups",
                "admin_state_up",
                "status",
            )
        )
        # Network and subnet should be writable for creation
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "openstack-port-detail"},
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
            network={"lookup_field": "uuid", "view_name": "openstack-network-detail"},
            subnet={"lookup_field": "uuid", "view_name": "openstack-subnet-detail"},
        )

    def validate(self, attrs):
        if self.instance:
            return attrs

        fixed_ips = attrs.get("fixed_ips")
        network = attrs.get("network")

        if not network:
            raise serializers.ValidationError(
                _("Network must be specified for creation of a port.")
            )

        if fixed_ips:
            for fixed_ip in fixed_ips:
                if "ip_address" not in fixed_ip and "subnet_id" not in fixed_ip:
                    raise serializers.ValidationError(
                        _("Either ip_address or subnet_id field must be specified")
                    )

                wrong_fields = set(fixed_ip.keys()) - {"ip_address", "subnet_id"}
                if wrong_fields != set():
                    raise serializers.ValidationError(
                        _(
                            "Only ip_address and subnet_id fields can be specified. Got: %(fields)s"
                        )
                        % {"fields": wrong_fields}
                    )

                if fixed_ip.get("ip_address") == "":
                    raise serializers.ValidationError(
                        _("ip_address field must not be blank. Got %(fixed_ip)s.")
                        % {"fixed_ip": fixed_ip}
                    )
                if "ip_address" in fixed_ip:
                    validate_ipv46_address(fixed_ip["ip_address"])

                subnet_backend_id = fixed_ip.get("subnet_id")
                if subnet_backend_id:
                    if not models.SubNet.objects.filter(
                        backend_id=subnet_backend_id, network=network
                    ).exists():
                        raise serializers.ValidationError(
                            {
                                "subnet": _(
                                    "There is no subnet with backend_id [%(backend_id)s] in the network [%(network)s]"
                                )
                                % {
                                    "backend_id": subnet_backend_id,
                                    "network": network,
                                }
                            }
                        )

        attrs["service_settings"] = network.service_settings
        attrs["project"] = network.project
        attrs["network"] = network

        # Use target_tenant if provided for shared networks, otherwise default to network.tenant
        target_tenant = attrs.get("target_tenant")
        if target_tenant:
            # Validate that the target tenant can access this network via RBAC
            if target_tenant != network.tenant:
                # Check if network is shared with target_tenant via RBAC policy
                rbac_exists = models.NetworkRBACPolicy.objects.filter(
                    network=network,
                    target_tenant=target_tenant,
                    policy_type__in=["access_as_shared", "access_as_external"],
                ).exists()

                if not rbac_exists:
                    raise serializers.ValidationError(
                        {
                            "target_tenant": _(
                                "Target tenant %(tenant)s does not have access to network %(network)s. "
                                "Network must be shared via RBAC policy."
                            )
                            % {
                                "tenant": target_tenant.uuid,
                                "network": network.name,
                            }
                        }
                    )

            attrs["tenant"] = target_tenant
            attrs["project"] = target_tenant.project
        else:
            attrs["tenant"] = network.tenant

        return super().validate(attrs)


class NetworkRBACPolicySerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    network = serializers.HyperlinkedRelatedField(
        view_name="openstack-network-detail",
        lookup_field="uuid",
        queryset=models.Network.objects.filter(state=CoreStates.OK).all(),
    )

    target_tenant = serializers.HyperlinkedRelatedField(
        view_name="openstack-tenant-detail",
        lookup_field="uuid",
        queryset=models.Tenant.objects.filter(state=CoreStates.OK).all(),
    )
    url = serializers.HyperlinkedIdentityField(
        view_name="openstack-network-rbac-policy-detail", lookup_field="uuid"
    )
    network_name = serializers.CharField(source="network.name", read_only=True)
    target_tenant_name = serializers.CharField(
        source="target_tenant.name", read_only=True
    )

    class Meta:
        model = models.NetworkRBACPolicy
        fields = (
            "url",
            "uuid",
            "network",
            "network_name",
            "target_tenant",
            "target_tenant_name",
            "backend_id",
            "policy_type",
            "created",
        )
        read_only_fields = ("uuid", "created", "backend_id")

    def validate_target_tenant(self, target_tenant):
        network = self.context.get("network")
        if (
            network
            and target_tenant.service_settings != network.tenant.service_settings
        ):
            raise serializers.ValidationError(
                _(
                    "Target tenant must belong to the same service settings as the network's tenant."
                )
            )
        return target_tenant

    def validate(self, attrs):
        attrs = super().validate(attrs)
        network = self.context.get("network")
        if network:
            target_tenant = attrs["target_tenant"]
            policy_type = attrs["policy_type"]

            # Check if policy with the same network, tenant and type already exists
            if models.NetworkRBACPolicy.objects.filter(
                network=network, target_tenant=target_tenant, policy_type=policy_type
            ).exists():
                raise serializers.ValidationError(
                    _(
                        "Policy with this network, target tenant and policy type already exists."
                    )
                )

        return attrs


class DeprecatedNetworkRBACPolicySerializer(NetworkRBACPolicySerializer):
    network = serializers.HyperlinkedRelatedField(
        view_name="openstack-network-detail",
        lookup_field="uuid",
        read_only=True,
    )


class OpenStackNetworkSerializer(
    structure_serializers.FieldFilteringMixin,
    structure_serializers.BaseResourceActionSerializer,
):
    subnets = OpenStackNestedSubNetSerializer(many=True, read_only=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    tenant_uuid = serializers.UUIDField(source="tenant.uuid", read_only=True)
    rbac_policies = NetworkRBACPolicySerializer(many=True, read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Network
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "is_external",
            "type",
            "segmentation_id",
            "subnets",
            "mtu",
            "rbac_policies",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "tenant",
                "is_external",
                "type",
                "segmentation_id",
                "mtu",
                "service_settings",
                "project",
                "rbac_policies",
            )
        )
        extra_kwargs = dict(
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs,
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs["tenant"] = tenant = self.context["view"].get_object()
        attrs["service_settings"] = tenant.service_settings
        attrs["project"] = tenant.project
        return super().validate(attrs)

    def get_filtered_field(self):
        return [
            (
                "segmentation_id",
                lambda user: (
                    user.is_authenticated and (user.is_staff or user.is_support)
                ),
            ),
        ]


class SetMtuSerializer(serializers.Serializer):
    mtu = serializers.IntegerField()

    def update(self, network, validated_data):
        network.mtu = validated_data["mtu"]
        network.save(update_fields=["mtu"])
        return network


@extend_schema_field(serializers.ListField(child=serializers.IPAddressField()))
class DnsNameserversField(serializers.JSONField):
    pass


class OpenStackSubNetSerializer(structure_serializers.BaseResourceActionSerializer):
    cidr = serializers.CharField(
        required=False,
        initial="192.168.42.0/24",
        label="CIDR",
    )
    allocation_pools = OpenStackSubNetAllocationPoolField(required=False)
    network_name = serializers.CharField(source="network.name", read_only=True)
    tenant = serializers.HyperlinkedRelatedField(
        source="network.tenant",
        view_name="openstack-tenant-detail",
        read_only=True,
        lookup_field="uuid",
    )
    tenant_name = serializers.CharField(source="network.tenant.name", read_only=True)
    dns_nameservers = DnsNameserversField(required=False)
    host_routes = OpenStackStaticRouteSerializer(many=True, required=False)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.SubNet
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "tenant",
            "tenant_name",
            "network",
            "network_name",
            "cidr",
            "gateway_ip",
            "disable_gateway",
            "allocation_pools",
            "ip_version",
            "enable_dhcp",
            "dns_nameservers",
            "host_routes",
            "is_connected",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "tenant",
                "network",
                "ip_version",
                "enable_dhcp",
                "service_settings",
                "project",
                "is_connected",
            )
        )
        extra_kwargs = dict(
            network={"lookup_field": "uuid", "view_name": "openstack-network-detail"},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs,
        )

    def get_fields(self):
        fields = super().get_fields()

        # Make cidr read-only on update
        if self.instance and "cidr" in fields:
            fields["cidr"].read_only = True

        return fields

    def validate(self, attrs):
        if attrs.get("disable_gateway") and attrs.get("gateway_ip"):
            raise serializers.ValidationError(
                _(
                    "These parameters are mutually exclusive: disable_gateway and gateway_ip."
                )
            )

        if attrs.get("disable_gateway"):
            # clean the gateway ip
            attrs["gateway_ip"] = None

        if "cidr" not in attrs:
            attrs["cidr"] = (
                "192.168.42.0/24"
                if not self.instance or not self.instance.cidr
                else self.instance.cidr
            )

        cidr = attrs["cidr"]
        allocation_pools = attrs.get("allocation_pools")

        if allocation_pools:
            # Check that each individual allocation pool is valid
            for allocation_pool in allocation_pools:
                ip_start = ip_address(allocation_pool["start"])
                ip_end = ip_address(allocation_pool["end"])
                if ip_start > ip_end:
                    raise serializers.ValidationError(
                        _("End IP must be larger than Start IP.")
                    )
                if ip_start not in ip_network(cidr, strict=False):
                    raise serializers.ValidationError(
                        _("Allocation pool does not match CIDR.")
                    )
                if ip_end not in ip_network(cidr, strict=False):
                    raise serializers.ValidationError(
                        _("Allocation pool does not match CIDR.")
                    )

            # Check for overlaps between allocation pools
            self.check_allocation_pools_overlap(allocation_pools)

        network = self.context["view"].get_object()

        # Only check CIDR overlap during subnet creation
        if self.instance is None:
            self.check_cidr_overlap(network.tenant, cidr)

            attrs["network"] = network
            attrs["tenant"] = network.tenant
            if network.subnets.count() >= 1:
                raise serializers.ValidationError(
                    _("Internal network cannot have more than one subnet.")
                )

            if models.SubNet.objects.filter(
                cidr=cidr, network__tenant=network.tenant
            ).exists():
                raise serializers.ValidationError(
                    _('Subnet with cidr "%s" is already registered') % cidr
                )

            attrs["service_settings"] = network.service_settings
            attrs["project"] = network.project
            options = network.service_settings.options
            attrs.setdefault("dns_nameservers", options.get("dns_nameservers", []))
        return attrs

    # Keep the previously defined methods below
    def check_cidr_overlap(self, tenant, new_cidr):
        """
        Check if the new CIDR overlaps with existing CIDRs.
        This is only used during subnet creation since CIDR cannot be updated.
        """
        # Get all subnets in the same tenant
        subnet_cidrs = models.SubNet.objects.filter(network__tenant=tenant).exclude(
            cidr=""
        )

        # Check each CIDR for overlap
        for subnet in subnet_cidrs:
            old_cidr = subnet.cidr
            try:
                old_ipnet = IPNetwork(old_cidr)
                new_ipnet = IPNetwork(new_cidr)

                # Check if either network contains the other
                if all_matching_cidrs(new_ipnet, [old_cidr]) or all_matching_cidrs(
                    old_ipnet, [new_cidr]
                ):
                    raise serializers.ValidationError(
                        _("CIDR %(new_cidr)s overlaps with CIDR %(old_cidr)s")
                        % dict(new_cidr=new_cidr, old_cidr=old_cidr)
                    )
            except (AddrFormatError, ValueError):
                # Skip invalid CIDRs
                continue

    def check_allocation_pools_overlap(self, allocation_pools):
        """
        Check if any of the allocation pools overlap with each other.

        Args:
            allocation_pools: List of dictionaries with 'start' and 'end' IP addresses

        Raises:
            ValidationError: If any allocation pools overlap
        """
        if not allocation_pools or len(allocation_pools) <= 1:
            return

        # Sort pools for easier comparison
        sorted_pools = sorted(allocation_pools, key=lambda p: ip_address(p["start"]))

        # Check for overlaps between adjacent pools
        for i in range(len(sorted_pools) - 1):
            current_end = ip_address(sorted_pools[i]["end"])
            next_start = ip_address(sorted_pools[i + 1]["start"])

            # If the end of the current pool is greater than or equal to the start of the next pool,
            # they overlap
            if current_end >= next_start:
                raise serializers.ValidationError(
                    _("Allocation pools overlap: %(pool1)s and %(pool2)s")
                    % {"pool1": sorted_pools[i], "pool2": sorted_pools[i + 1]}
                )

    def update(self, instance, validated_data):
        host_routes = validated_data.pop("host_routes", [])
        instance = super().update(instance, validated_data)
        instance.host_routes = host_routes
        instance.save()
        return instance


def _generate_subnet_allocation_pool(cidr):
    network = ip_network(cidr, strict=False)
    first_host = network.network_address + 2
    last_host = network.broadcast_address - 1
    return [
        {
            "start": str(first_host),
            "end": str(last_host),
        }
    ]


class OpenStackTenantChangePasswordSerializer(serializers.Serializer):
    user_password = serializers.CharField(
        max_length=50,
        allow_blank=True,
        validators=[password_validation.validate_password],
        help_text=_("New tenant user password."),
    )

    def validate_user_password(self, user_password):
        if self.instance.user_password == user_password:
            raise serializers.ValidationError(
                _("New password cannot match the old password.")
            )

        return user_password

    def update(self, tenant, validated_data):
        tenant.user_password = validated_data["user_password"]
        tenant.save(update_fields=["user_password"])
        return tenant


class OpenStackNestedPortSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    url = serializers.HyperlinkedIdentityField(
        view_name="openstack-port-detail", lookup_field="uuid"
    )
    allowed_address_pairs = OpenStackAllowedAddressPairField(read_only=True)
    fixed_ips = OpenStackFixedIpField(required=False)
    security_groups = OpenStackSecurityGroupSerializer(
        read_only=True,
        many=True,
    )

    class Meta:
        model = models.Port
        fields = (
            "url",
            "fixed_ips",
            "mac_address",
            "subnet",
            "subnet_uuid",
            "subnet_name",
            "subnet_description",
            "subnet_cidr",
            "allowed_address_pairs",
            "device_id",
            "device_owner",
            "security_groups",
        )
        read_only_fields = (
            "mac_address",
            "subnet_uuid",
            "subnet_name",
            "subnet_description",
            "subnet_cidr",
            "allowed_address_pairs",
            "device_id",
            "device_owner",
        )
        related_paths = {
            "subnet": ("uuid", "name", "description", "cidr"),
        }
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "openstack-port-detail"},
            "subnet": {
                "lookup_field": "uuid",
                "view_name": "openstack-subnet-detail",
            },
        }


class OpenStackCreatePortSerializer(serializers.HyperlinkedModelSerializer):
    fixed_ips = OpenStackFixedIpField(required=False)
    port = serializers.HyperlinkedRelatedField(
        view_name="openstack-port-detail",
        lookup_field="uuid",
        queryset=models.Port.objects.all(),
        required=False,
    )
    tenant = serializers.HyperlinkedRelatedField(
        view_name="openstack-tenant-detail",
        lookup_field="uuid",
        queryset=models.Tenant.objects.filter(state=CoreStates.OK).all(),
        write_only=True,
        required=False,
        help_text="Target tenant for port creation. If not specified, uses subnet's tenant.",
    )

    class Meta:
        model = models.Port
        fields = (
            "fixed_ips",
            "subnet",
            "port",
            "tenant",
        )
        extra_kwargs = {
            "subnet": {
                "lookup_field": "uuid",
                "view_name": "openstack-subnet-detail",
            },
        }

    def validate_fixed_ips(self, value):
        OpenStackFixedIpSerializer(data=value, many=True).is_valid(raise_exception=True)
        return value

    def to_internal_value(self, data):
        internal_value = super().to_internal_value(data)
        port: models.Port | None = internal_value.get("port")

        if port:
            return port

        subnet: models.SubNet = internal_value.get("subnet")
        fixed_ips = internal_value.get("fixed_ips")

        # For instance creation, we need to determine the correct tenant
        # The tenant should be set by the parent instance serializer context
        # If not available, fall back to subnet.tenant (original behavior)
        instance_tenant = None
        if hasattr(self, "context") and self.context:
            # Try to get instance tenant from parent serializer context
            parent_serializer = self.context.get("parent_serializer")
            if parent_serializer and hasattr(parent_serializer, "validated_data"):
                instance_tenant = parent_serializer.validated_data.get("tenant")

        # Use instance tenant if available, otherwise fall back to subnet tenant
        port_tenant = instance_tenant if instance_tenant else subnet.tenant
        port_project = instance_tenant.project if instance_tenant else subnet.project

        return models.Port(
            subnet=subnet,
            network=subnet.network,
            tenant=port_tenant,
            project=port_project,
            service_settings=subnet.service_settings,
            fixed_ips=fixed_ips,
        )


class OpenStackCreateInstancePortSerializer(serializers.HyperlinkedModelSerializer):
    """
    Port serializer specifically for instance creation that handles shared networks correctly.
    Always assigns ports to the instance's tenant, not the network's tenant.
    """

    fixed_ips = OpenStackFixedIpField(required=False)
    port = serializers.HyperlinkedRelatedField(
        view_name="openstack-port-detail",
        lookup_field="uuid",
        queryset=models.Port.objects.all(),
        required=False,
    )

    class Meta:
        model = models.Port
        fields = (
            "fixed_ips",
            "subnet",
            "port",
            "port_security_enabled",
        )
        extra_kwargs = {
            "subnet": {
                "lookup_field": "uuid",
                "view_name": "openstack-subnet-detail",
            },
        }

    def validate_fixed_ips(self, value):
        OpenStackFixedIpSerializer(data=value, many=True).is_valid(raise_exception=True)
        return value

    def to_internal_value(self, data):
        internal_value = super().to_internal_value(data)
        port: models.Port | None = internal_value.get("port")

        if port:
            return port

        subnet: models.SubNet = internal_value.get("subnet")
        fixed_ips = internal_value.get("fixed_ips")
        port_security_enabled = internal_value.get("port_security_enabled", True)

        # For instance creation, initially set to subnet's tenant
        # This will be corrected to instance's tenant during instance creation
        return models.Port(
            subnet=subnet,
            network=subnet.network,
            tenant=subnet.tenant,  # Initially use subnet's tenant (will be corrected later)
            project=subnet.project,  # Initially use subnet's project (will be corrected later)
            service_settings=subnet.service_settings,
            fixed_ips=fixed_ips,
            port_security_enabled=port_security_enabled,
        )


class OpenStackRouterSerializer(structure_serializers.BaseResourceSerializer):
    routes = OpenStackStaticRouteSerializer(many=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    tenant_uuid = serializers.UUIDField(source="tenant.uuid", read_only=True)
    fixed_ips = OpenStackFixedIpField(read_only=True)
    ports = OpenStackNestedPortSerializer(many=True, read_only=True)
    has_external_gateway = serializers.BooleanField(read_only=True)
    external_network_uuid = serializers.UUIDField(
        source="external_network_ref.uuid", read_only=True, allow_null=True
    )
    external_network_name = serializers.CharField(
        source="external_network_ref.name", read_only=True, allow_null=True
    )
    external_fixed_ips = serializers.JSONField(read_only=True)

    class Meta:
        model = models.Router
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "routes",
            "fixed_ips",
            "ports",
            "external_network_id",
            "external_network_uuid",
            "external_network_name",
            "has_external_gateway",
            "enable_snat",
            "external_fixed_ips",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "openstack-router-detail"},
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
        )


class OpenStackLoadBalancerVIPSecurityGroupSerializer(serializers.Serializer):
    uuid = serializers.CharField()
    name = serializers.CharField()
    url = serializers.URLField()


class OpenStackLoadBalancerSerializer(structure_serializers.BaseResourceSerializer):
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    tenant_uuid = serializers.UUIDField(source="tenant.uuid", read_only=True)
    vip_address = serializers.IPAddressField(read_only=True)
    vip_subnet = serializers.HyperlinkedRelatedField(
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
        read_only=True,
        allow_null=True,
    )
    vip_port = serializers.HyperlinkedRelatedField(
        view_name="openstack-port-detail",
        lookup_field="uuid",
        read_only=True,
        allow_null=True,
    )
    provider = serializers.CharField(read_only=True)
    provisioning_status = serializers.CharField(read_only=True)
    operating_status = serializers.CharField(read_only=True)
    vip_security_groups = serializers.SerializerMethodField()

    @extend_schema_field(
        OpenStackLoadBalancerVIPSecurityGroupSerializer(
            many=True,
            help_text="Security groups assigned to the VIP port.",
        )
    )
    def get_vip_security_groups(self, obj):
        if not obj.vip_port:
            return []
        return [
            {
                "uuid": str(sg.uuid),
                "name": sg.name,
                "url": reverse(
                    "openstack-sgp-detail",
                    kwargs={"uuid": sg.uuid},
                    request=self.context.get("request"),
                ),
            }
            for sg in obj.vip_port.security_groups.all()
        ]

    class Meta:
        model = models.LoadBalancer
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "vip_address",
            "vip_subnet",
            "vip_port",
            "attached_floating_ip",
            "provider",
            "provisioning_status",
            "operating_status",
            "vip_security_groups",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "openstack-loadbalancer-detail"},
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
            attached_floating_ip={
                "lookup_field": "uuid",
                "view_name": "openstack-fip-detail",
            },
        )


class LoadBalancerAttachFloatingIPSerializer(serializers.Serializer):
    floating_ip = serializers.HyperlinkedRelatedField(
        view_name="openstack-fip-detail",
        lookup_field="uuid",
        queryset=models.FloatingIP.objects.all(),
    )


class LoadBalancerAsyncOperationResponseSerializer(serializers.Serializer):
    """Response body when a load balancer backend operation is accepted (HTTP 202)."""

    status = serializers.CharField(
        help_text="Message that execution of the operation was scheduled.",
    )


class LoadBalancerSetSecurityGroupsSerializer(serializers.Serializer):
    security_groups = serializers.ListField(
        child=serializers.HyperlinkedRelatedField(
            view_name="openstack-sgp-detail",
            lookup_field="uuid",
            queryset=models.SecurityGroup.objects.all(),
        ),
    )


class LoadBalancerWritableSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField()
    uuid = serializers.UUIDField(read_only=True)
    url = serializers.HyperlinkedIdentityField(
        view_name="openstack-loadbalancer-detail", lookup_field="uuid"
    )

    class Meta:
        model = models.LoadBalancer
        fields = (
            "url",
            "uuid",
            "name",
        )


class UpdateLoadBalancerSerializer(LoadBalancerWritableSerializer):
    pass


class CreateLoadBalancerSerializer(LoadBalancerWritableSerializer):
    name = serializers.CharField()
    vip_subnet = serializers.HyperlinkedRelatedField(
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
        queryset=models.SubNet.objects.all(),
        required=True,
    )

    class Meta(LoadBalancerWritableSerializer.Meta):
        fields = LoadBalancerWritableSerializer.Meta.fields + (
            "tenant",
            "vip_subnet",
        )
        extra_kwargs = dict(
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
        )

    def validate_tenant(self, tenant):
        user = self.context["request"].user
        if not (
            user.is_staff
            or tenant.project.customer.has_user(user, CustomerRole.OWNER)
            or tenant.project.has_user(user, ProjectRole.ADMIN)
            or tenant.project.has_user(user, ProjectRole.MANAGER)
        ):
            raise serializers.ValidationError(
                "You do not have permission to create load balancer for this tenant."
            )

        if tenant.state != CoreStates.OK:
            raise serializers.ValidationError(
                "Load balancer can be created only for tenant in OK state."
            )

        return tenant

    def validate(self, attrs):
        attrs = super().validate(attrs)
        tenant = attrs.get("tenant")
        subnet = attrs["vip_subnet"]
        if subnet.tenant_id != tenant.id:
            raise serializers.ValidationError(
                {"vip_subnet": _("Subnet must belong to the selected tenant.")}
            )
        if not subnet.backend_id:
            raise serializers.ValidationError(
                {
                    "vip_subnet": _(
                        "Subnet must be provisioned in the backend before creating a load balancer."
                    )
                }
            )
        attrs["project"] = tenant.project
        attrs["service_settings"] = tenant.service_settings
        return attrs


class OpenStackPoolSerializer(structure_serializers.BaseResourceSerializer):
    load_balancer_name = serializers.CharField(
        source="load_balancer.name", read_only=True
    )
    load_balancer_uuid = serializers.UUIDField(
        source="load_balancer.uuid", read_only=True
    )
    protocol = serializers.CharField(read_only=True)
    lb_algorithm = serializers.CharField(read_only=True)
    provisioning_status = serializers.CharField(read_only=True)
    operating_status = serializers.CharField(read_only=True)

    class Meta:
        model = models.Pool
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "load_balancer",
            "load_balancer_name",
            "load_balancer_uuid",
            "protocol",
            "lb_algorithm",
            "provisioning_status",
            "operating_status",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "openstack-pool-detail"},
            load_balancer={
                "lookup_field": "uuid",
                "view_name": "openstack-loadbalancer-detail",
            },
        )


def _validate_load_balancer(load_balancer, user):
    if not (
        user.is_staff
        or load_balancer.project.customer.has_user(user, CustomerRole.OWNER)
        or load_balancer.project.has_user(user, ProjectRole.ADMIN)
        or load_balancer.project.has_user(user, ProjectRole.MANAGER)
    ):
        raise serializers.ValidationError(
            "You do not have permission to create pool for this load balancer."
        )

    if load_balancer.state != CoreStates.OK:
        raise serializers.ValidationError(
            "Pool can be created only for load balancer in OK state."
        )

    if not load_balancer.backend_id:
        raise serializers.ValidationError(
            "Load balancer must be provisioned in the backend before creating a pool."
        )


class PoolWritableSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField()
    uuid = serializers.UUIDField(read_only=True)
    url = serializers.HyperlinkedIdentityField(
        view_name="openstack-pool-detail", lookup_field="uuid"
    )

    class Meta:
        model = models.Pool
        fields = (
            "url",
            "uuid",
            "name",
        )


class UpdatePoolSerializer(PoolWritableSerializer):
    pass


class CreatePoolSerializer(PoolWritableSerializer):
    protocol = serializers.ChoiceField(choices=models.PROTOCOL_CHOICES)
    lb_algorithm = serializers.ChoiceField(
        choices=models.LB_ALGORITHM_CHOICES,
        default="SOURCE_IP_PORT",
        required=False,
    )

    class Meta(PoolWritableSerializer.Meta):
        fields = PoolWritableSerializer.Meta.fields + (
            "load_balancer",
            "protocol",
            "lb_algorithm",
        )
        extra_kwargs = dict(
            load_balancer={
                "lookup_field": "uuid",
                "view_name": "openstack-loadbalancer-detail",
            },
        )

    def validate_load_balancer(self, load_balancer):
        user = self.context["request"].user
        _validate_load_balancer(load_balancer, user)
        return load_balancer

    def validate(self, attrs):
        attrs = super().validate(attrs)
        load_balancer = attrs["load_balancer"]
        lb_algorithm = attrs.get("lb_algorithm", "SOURCE_IP_PORT")
        if (
            load_balancer.provider == "ovn"
            and lb_algorithm not in models.OVN_SUPPORTED_LB_ALGORITHMS
        ):
            raise serializers.ValidationError(
                {
                    "lb_algorithm": _(
                        "OVN provider only supports the following algorithms: %s."
                    )
                    % ", ".join(models.OVN_SUPPORTED_LB_ALGORITHMS)
                }
            )
        attrs["project"] = load_balancer.project
        attrs["service_settings"] = load_balancer.service_settings
        return attrs


class OpenStackListenerSerializer(structure_serializers.BaseResourceSerializer):
    load_balancer_name = serializers.CharField(
        source="load_balancer.name", read_only=True
    )
    load_balancer_uuid = serializers.UUIDField(
        source="load_balancer.uuid", read_only=True
    )
    protocol = serializers.CharField(read_only=True)
    protocol_port = serializers.IntegerField(read_only=True)
    provisioning_status = serializers.CharField(read_only=True)
    operating_status = serializers.CharField(read_only=True)

    class Meta:
        model = models.Listener
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "load_balancer",
            "load_balancer_name",
            "load_balancer_uuid",
            "protocol",
            "protocol_port",
            "default_pool",
            "provisioning_status",
            "operating_status",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "openstack-listener-detail"},
            load_balancer={
                "lookup_field": "uuid",
                "view_name": "openstack-loadbalancer-detail",
            },
            default_pool={
                "lookup_field": "uuid",
                "view_name": "openstack-pool-detail",
            },
        )


class ListenerWritableSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(required=False)
    default_pool = serializers.HyperlinkedRelatedField(
        view_name="openstack-pool-detail",
        lookup_field="uuid",
        queryset=models.Pool.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = models.Listener
        fields = ("name", "default_pool")
        extra_kwargs = dict(
            default_pool={
                "lookup_field": "uuid",
                "view_name": "openstack-pool-detail",
            },
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if "default_pool" not in attrs:
            return attrs
        default_pool = attrs["default_pool"]

        if default_pool is None:
            return attrs

        if self.instance is not None:
            load_balancer = self.instance.load_balancer
        else:
            load_balancer = attrs.get("load_balancer")

        if default_pool.load_balancer_id != load_balancer.id:
            raise serializers.ValidationError(
                {
                    "default_pool": _(
                        "Default pool must belong to the same load balancer."
                    )
                }
            )

        if not default_pool.backend_id:
            raise serializers.ValidationError(
                {"default_pool": _("Default pool must be provisioned in the backend.")}
            )

        return attrs


class UpdateListenerSerializer(ListenerWritableSerializer):
    pass


class CreateListenerSerializer(ListenerWritableSerializer):
    protocol = serializers.ChoiceField(choices=models.PROTOCOL_CHOICES)
    protocol_port = serializers.IntegerField(
        min_value=1, max_value=65535, help_text="Port on which the listener listens"
    )
    uuid = serializers.UUIDField(read_only=True)
    url = serializers.HyperlinkedIdentityField(
        view_name="openstack-listener-detail", lookup_field="uuid"
    )

    class Meta(ListenerWritableSerializer.Meta):
        fields = ListenerWritableSerializer.Meta.fields + (
            "url",
            "uuid",
            "load_balancer",
            "protocol",
            "protocol_port",
        )

        extra_kwargs = {
            **ListenerWritableSerializer.Meta.extra_kwargs,
            "load_balancer": {
                "lookup_field": "uuid",
                "view_name": "openstack-loadbalancer-detail",
            },
        }

    def validate_load_balancer(self, load_balancer):
        user = self.context["request"].user
        _validate_load_balancer(load_balancer, user)
        return load_balancer

    def validate(self, attrs):
        attrs = super().validate(attrs)
        load_balancer = attrs["load_balancer"]
        attrs["project"] = load_balancer.project
        attrs["service_settings"] = load_balancer.service_settings
        return attrs


class OpenStackPoolMemberSerializer(structure_serializers.BaseResourceSerializer):
    pool_name = serializers.CharField(source="pool.name", read_only=True)
    pool_uuid = serializers.UUIDField(source="pool.uuid", read_only=True)
    load_balancer_uuid = serializers.UUIDField(
        source="pool.load_balancer.uuid", read_only=True
    )
    address = serializers.IPAddressField(read_only=True)
    protocol_port = serializers.IntegerField(read_only=True)
    subnet = serializers.HyperlinkedRelatedField(
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
        read_only=True,
        allow_null=True,
    )
    weight = serializers.IntegerField(read_only=True)
    provisioning_status = serializers.CharField(read_only=True)
    operating_status = serializers.CharField(read_only=True)

    class Meta:
        model = models.PoolMember
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "pool",
            "pool_name",
            "pool_uuid",
            "load_balancer_uuid",
            "address",
            "protocol_port",
            "subnet",
            "weight",
            "provisioning_status",
            "operating_status",
        )
        extra_kwargs = dict(
            url={
                "lookup_field": "uuid",
                "view_name": "openstack-poolmember-detail",
            },
            pool={
                "lookup_field": "uuid",
                "view_name": "openstack-pool-detail",
            },
        )


class PoolMemberWritingSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(required=False, allow_blank=True)
    weight = serializers.IntegerField(
        min_value=1, max_value=256, default=1, required=False
    )
    uuid = serializers.UUIDField(read_only=True)
    url = serializers.HyperlinkedIdentityField(
        view_name="openstack-poolmember-detail", lookup_field="uuid"
    )

    class Meta:
        model = models.PoolMember
        fields = (
            "url",
            "uuid",
            "name",
            "weight",
        )


class UpdatePoolMemberSerializer(PoolMemberWritingSerializer):
    pass


class CreatePoolMemberSerializer(PoolMemberWritingSerializer):
    address = serializers.IPAddressField()
    protocol_port = serializers.IntegerField(
        min_value=1,
        max_value=65535,
        help_text="Port on the backend server",
    )
    subnet = serializers.HyperlinkedRelatedField(
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
        queryset=models.SubNet.objects.all(),
        required=True,
    )

    class Meta(PoolMemberWritingSerializer.Meta):
        fields = PoolMemberWritingSerializer.Meta.fields + (
            "pool",
            "address",
            "protocol_port",
            "subnet",
        )
        extra_kwargs = dict(
            pool={
                "lookup_field": "uuid",
                "view_name": "openstack-pool-detail",
            },
        )

    def validate_pool(self, pool):
        user = self.context["request"].user
        if not (
            user.is_staff
            or pool.project.customer.has_user(user, CustomerRole.OWNER)
            or pool.project.has_user(user, ProjectRole.ADMIN)
            or pool.project.has_user(user, ProjectRole.MANAGER)
        ):
            raise serializers.ValidationError(
                "You do not have permission to create member for this pool."
            )

        if pool.state != CoreStates.OK:
            raise serializers.ValidationError(
                "Member can be created only for pool in OK state."
            )

        if not pool.backend_id:
            raise serializers.ValidationError(
                "Pool must be provisioned in the backend before creating a member."
            )

        return pool

    def validate_subnet(self, subnet):
        if not subnet.backend_id:
            raise serializers.ValidationError(
                _("Subnet must be provisioned in the backend before creating a member.")
            )
        return subnet

    def validate(self, attrs):
        attrs = super().validate(attrs)
        pool = attrs["pool"]
        subnet = attrs["subnet"]
        if subnet.tenant_id != pool.load_balancer.tenant_id:
            raise serializers.ValidationError(
                {
                    "subnet": _(
                        "Subnet must belong to the same tenant as the load balancer."
                    )
                }
            )
        attrs["project"] = pool.project
        attrs["service_settings"] = pool.service_settings
        return attrs


class OpenStackHealthMonitorSerializer(structure_serializers.BaseResourceSerializer):
    pool_name = serializers.CharField(source="pool.name", read_only=True)
    pool_uuid = serializers.UUIDField(source="pool.uuid", read_only=True)
    load_balancer_uuid = serializers.UUIDField(
        source="pool.load_balancer.uuid", read_only=True
    )
    type = serializers.CharField(source="monitor_type", read_only=True)
    delay = serializers.IntegerField(read_only=True)
    timeout = serializers.IntegerField(read_only=True)
    max_retries = serializers.IntegerField(read_only=True)
    provisioning_status = serializers.CharField(read_only=True)
    operating_status = serializers.CharField(read_only=True)

    class Meta:
        model = models.HealthMonitor
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "pool",
            "pool_name",
            "pool_uuid",
            "load_balancer_uuid",
            "type",
            "delay",
            "timeout",
            "max_retries",
            "provisioning_status",
            "operating_status",
        )
        extra_kwargs = dict(
            url={
                "lookup_field": "uuid",
                "view_name": "openstack-healthmonitor-detail",
            },
            pool={
                "lookup_field": "uuid",
                "view_name": "openstack-pool-detail",
            },
        )


class HealthMonitorWritableSerializer(serializers.HyperlinkedModelSerializer):
    uuid = serializers.UUIDField(read_only=True)
    url = serializers.HyperlinkedIdentityField(
        view_name="openstack-healthmonitor-detail", lookup_field="uuid"
    )
    name = serializers.CharField(required=False, allow_blank=True)
    delay = serializers.IntegerField(
        min_value=1,
        help_text="Interval between health checks in seconds",
        default=5,
    )
    timeout = serializers.IntegerField(
        min_value=1, help_text="Time in seconds to timeout a health check", default=5
    )
    max_retries = serializers.IntegerField(
        min_value=1,
        max_value=10,
        default=3,
    )
    max_retries_down = serializers.IntegerField(
        min_value=1,
        max_value=10,
        default=3,
    )

    class Meta:
        model = models.HealthMonitor
        fields = (
            "url",
            "uuid",
            "name",
            "delay",
            "timeout",
            "max_retries",
            "max_retries_down",
        )


class UpdateHealthMonitorSerializer(HealthMonitorWritableSerializer):
    pass


class CreateHealthMonitorSerializer(HealthMonitorWritableSerializer):
    type = serializers.ChoiceField(
        choices=models.HEALTHMONITOR_TYPE_CHOICES, source="monitor_type"
    )

    class Meta(HealthMonitorWritableSerializer.Meta):
        fields = HealthMonitorWritableSerializer.Meta.fields + (
            "pool",
            "type",
        )
        extra_kwargs = dict(
            pool={
                "lookup_field": "uuid",
                "view_name": "openstack-pool-detail",
            },
        )

    def validate_pool(self, pool):
        user = self.context["request"].user
        if not (
            user.is_staff
            or pool.project.customer.has_user(user, CustomerRole.OWNER)
            or pool.project.has_user(user, ProjectRole.ADMIN)
            or pool.project.has_user(user, ProjectRole.MANAGER)
        ):
            raise serializers.ValidationError(
                "You do not have permission to create health monitor for this pool."
            )

        if pool.state != CoreStates.OK:
            raise serializers.ValidationError(
                "Health monitor can be created only for pool in OK state."
            )

        if not pool.backend_id:
            raise serializers.ValidationError(
                "Pool must be provisioned in the backend before creating a health monitor."
            )

        if models.HealthMonitor.objects.filter(pool=pool).exists():
            raise serializers.ValidationError("Pool already has a health monitor.")

        return pool

    def validate(self, attrs):
        attrs = super().validate(attrs)
        pool = attrs["pool"]
        attrs["project"] = pool.project
        attrs["service_settings"] = pool.service_settings
        return attrs


class CreateRouterSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField()
    project = serializers.HyperlinkedRelatedField(
        view_name="project-detail",
        lookup_field="uuid",
        read_only=True,
    )
    service_settings = serializers.HyperlinkedRelatedField(
        view_name="servicesettings-detail",
        lookup_field="uuid",
        read_only=True,
    )
    uuid = serializers.UUIDField(read_only=True)
    url = serializers.HyperlinkedIdentityField(
        view_name="openstack-router-detail", lookup_field="uuid"
    )

    class Meta:
        model = models.Router
        fields = (
            "url",
            "uuid",
            "tenant",
            "name",
            "project",
            "service_settings",
        )
        extra_kwargs = dict(
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
        )

    def validate_tenant(self, tenant):
        user = self.context["request"].user
        if not (
            user.is_staff
            or tenant.project.customer.has_user(user, CustomerRole.OWNER)
            or tenant.project.has_user(user, ProjectRole.ADMIN)
            or tenant.project.has_user(user, ProjectRole.MANAGER)
        ):
            raise serializers.ValidationError(
                "You do not have permission to create router for this tenant."
            )

        if tenant.state != CoreStates.OK:
            raise serializers.ValidationError(
                "Router can be created only for tenant in OK state."
            )

        return tenant

    def validate(self, attrs):
        attrs = super().validate(attrs)
        tenant = attrs.get("tenant")
        attrs["project"] = tenant.project
        attrs["service_settings"] = tenant.service_settings
        return attrs


class OpenStackNestedFloatingIPSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    subnet = serializers.HyperlinkedRelatedField(
        queryset=models.SubNet.objects.all(),
        source="port.subnet",
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
    )
    subnet_uuid = serializers.UUIDField(read_only=True, source="port.subnet.uuid")
    subnet_name = serializers.ReadOnlyField(source="port.subnet.name")
    subnet_description = serializers.ReadOnlyField(source="port.subnet.description")
    subnet_cidr = serializers.ReadOnlyField(source="port.subnet.cidr")
    port_fixed_ips = OpenStackFixedIpField(source="port.fixed_ips", read_only=True)

    class Meta:
        model = models.FloatingIP
        fields = (
            "url",
            "uuid",
            "address",
            "port_fixed_ips",
            "port_mac_address",
            "subnet",
            "subnet_uuid",
            "subnet_name",
            "subnet_description",
            "subnet_cidr",
        )
        read_only_fields = (
            "address",
            "port_fixed_ips",
            "port_mac_address",
        )
        related_paths = {"port": ("fixed_ips", "mac_address")}
        extra_kwargs = {
            "url": {"lookup_field": "uuid", "view_name": "openstack-fip-detail"},
        }


class OpenStackCreateFloatingIPSerializer(serializers.Serializer):
    url = serializers.HyperlinkedRelatedField(
        queryset=models.FloatingIP.objects.all(),
        view_name="openstack-fip-detail",
        lookup_field="uuid",
        required=False,
    )
    ip_address = serializers.IPAddressField(
        required=False,
        help_text="Existing floating IP address in selected OpenStack tenant to be assigned to new virtual machine",
    )
    subnet = serializers.HyperlinkedRelatedField(
        queryset=models.SubNet.objects.all(),
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
    )

    def to_internal_value(self, data):
        # Run standard field-level validation and conversion first
        validated_data = super().to_internal_value(data)

        # Perform object-level validation that was previously in the `validate` method.
        if validated_data.get("url") and validated_data.get("ip_address"):
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Please specify floating IP URL or IP address, not both"
                    ]
                }
            )

        # Process the validated data to find the floating IP and subnet
        subnet: models.SubNet = validated_data["subnet"]
        ip_address = validated_data.get("ip_address")

        # After conversion, the HyperlinkedRelatedField stores the object under its field name 'url'.
        floating_ip = validated_data.get("url")

        if not floating_ip and ip_address:
            try:
                floating_ip = models.FloatingIP.objects.get(
                    tenant=subnet.tenant, address=ip_address
                )
            except models.FloatingIP.DoesNotExist:
                # If IP does not exist in the database, a new one should be allocated.
                # We pass here, so `floating_ip` remains None, signaling allocation.
                pass

        # The view expects a tuple of (FloatingIP instance or None, SubNet instance)
        return (floating_ip, subnet)


class OpenStackUsageStatsSerializer(serializers.Serializer):
    shared = serializers.BooleanField()
    service_provider = serializers.ListField(child=serializers.CharField())


class BaseAvailabilityZoneSerializer(structure_serializers.BasePropertySerializer):
    settings = serializers.HyperlinkedRelatedField(
        queryset=structure_models.ServiceSettings.objects.all(),
        view_name="servicesettings-detail",
        lookup_field="uuid",
        allow_null=True,
        required=False,
    )

    class Meta(structure_serializers.BasePropertySerializer.Meta):
        fields = ("url", "uuid", "name", "settings", "available")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "settings": {"lookup_field": "uuid"},
        }


class OpenStackVolumeAvailabilityZoneSerializer(BaseAvailabilityZoneSerializer):
    class Meta(BaseAvailabilityZoneSerializer.Meta):
        model = models.VolumeAvailabilityZone


class OpenStackVolumeSerializer(structure_serializers.BaseResourceSerializer):
    action_details = serializers.JSONField(read_only=True)
    metadata = serializers.JSONField(read_only=True)
    instance_name = serializers.ReadOnlyField(source="instance.name")
    instance_marketplace_uuid = serializers.UUIDField(
        read_only=True, source="instance.marketplace_uuid"
    )
    type_name = serializers.CharField(source="type.name", read_only=True)
    availability_zone_name = serializers.CharField(
        source="availability_zone.name", read_only=True
    )
    tenant = serializers.HyperlinkedRelatedField(
        view_name="openstack-tenant-detail",
        lookup_field="uuid",
        queryset=models.Tenant.objects.all(),
    )
    service_settings = serializers.HyperlinkedRelatedField(
        read_only=True,
        view_name="servicesettings-detail",
        lookup_field="uuid",
    )
    tenant_uuid = serializers.UUIDField(read_only=True, source="tenant.uuid")
    extend_enabled = serializers.BooleanField(read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Volume
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "source_snapshot",
            "size",
            "bootable",
            "metadata",
            "image",
            "image_metadata",
            "image_name",
            "type",
            "type_name",
            "runtime_state",
            "availability_zone",
            "availability_zone_name",
            "device",
            "action",
            "action_details",
            "instance",
            "instance_name",
            "instance_marketplace_uuid",
            "tenant",
            "tenant_uuid",
            "extend_enabled",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "image_metadata",
                "image_name",
                "source_snapshot",
                "runtime_state",
                "device",
                "metadata",
                "action",
                "instance",
            )
        )
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + (
                "size",
                "image",
                "type",
                "availability_zone",
                "tenant",
            )
        )
        extra_kwargs = dict(
            instance={
                "lookup_field": "uuid",
                "view_name": "openstack-instance-detail",
            },
            image={"lookup_field": "uuid", "view_name": "openstack-image-detail"},
            source_snapshot={
                "lookup_field": "uuid",
                "view_name": "openstack-snapshot-detail",
            },
            type={
                "lookup_field": "uuid",
                "view_name": "openstack-volume-type-detail",
            },
            availability_zone={
                "lookup_field": "uuid",
                "view_name": "openstack-volume-availability-zone-detail",
            },
            size={"required": False, "allow_null": True},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs,
        )

    def validate(self, attrs):
        attrs = super().validate(attrs)

        if self.instance is None:
            # image validation
            image = attrs.get("image")
            tenant: models.Tenant = attrs["tenant"]
            if image and not is_image_valid_for_tenant(image, tenant):
                raise serializers.ValidationError(
                    {"image": _("Image is not visible in tenant.")}
                )
            # snapshot & size validation
            size = attrs.get("size")
            snapshot = attrs.get("snapshot")
            if not size and not snapshot:
                raise serializers.ValidationError(
                    _("Snapshot or size should be defined")
                )
            if size and snapshot:
                raise serializers.ValidationError(
                    _("It is impossible to define both snapshot and size")
                )
            # image & size validation
            size = size or snapshot.size
            if image and image.min_disk > size:
                raise serializers.ValidationError(
                    {
                        "size": _(
                            "Volume size should be equal or greater than %s for selected image"
                        )
                        % image.min_disk
                    }
                )
            # type validation
            volume_type = attrs.get("type")
            if volume_type and not is_volume_type_valid_for_tenant(volume_type, tenant):
                raise serializers.ValidationError(
                    {"type": _("Volume type is not visible in tenant.")}
                )

            availability_zone = attrs.get("availability_zone")
            if availability_zone and availability_zone.tenant != tenant:
                raise serializers.ValidationError(
                    _("Availability zone must belong to the same tenant.")
                )
            if availability_zone and not availability_zone.available:
                raise serializers.ValidationError(_("Zone is not available."))
            if (
                not availability_zone
                and settings.WALDUR_OPENSTACK["REQUIRE_AVAILABILITY_ZONE"]
            ):
                if (
                    models.VolumeAvailabilityZone.objects.filter(tenant=tenant).count()
                    > 0
                ):
                    raise serializers.ValidationError(
                        _("Availability zone is mandatory.")
                    )

        return attrs

    def create(self, validated_data):
        if not validated_data.get("size"):
            validated_data["size"] = validated_data["snapshot"].size
        if validated_data.get("image"):
            validated_data["image_name"] = validated_data["image"].name
        validated_data["service_settings"] = validated_data["tenant"].service_settings
        return super().create(validated_data)


class OpenStackVolumeExtendSerializer(serializers.Serializer):
    disk_size = serializers.IntegerField(min_value=1, label="Disk size")

    def validate_disk_size(self, disk_size):
        if disk_size < self.instance.size + 1024:
            raise serializers.ValidationError(
                _("Disk size should be greater or equal to %s")
                % (self.instance.size + 1024)
            )
        return disk_size

    @transaction.atomic
    def update(self, instance: models.Volume, validated_data):
        new_size = validated_data["disk_size"]

        instance.tenant.add_quota_usage(
            "storage", new_size - instance.size, validate=True
        )
        if instance.type:
            key = volume_type_name_to_quota_name(instance.type.name)
            delta = (new_size - instance.size) / 1024
            instance.tenant.add_quota_usage(key, delta, validate=True)

        instance.size = new_size
        instance.save(update_fields=["size"])
        return instance


class VolumeAttachSerializer(
    structure_serializers.PermissionFieldFilteringMixin,
    serializers.HyperlinkedModelSerializer,
):
    class Meta:
        model = models.Volume
        fields = ["instance"]
        extra_kwargs = dict(
            instance={
                "required": True,
                "allow_null": False,
                "view_name": "openstack-instance-detail",
                "lookup_field": "uuid",
            }
        )

    def get_filtered_field_names(self):
        return ("instance",)

    def validate_instance(self, instance):
        States, RuntimeStates = (
            CoreStates,
            models.Instance.RuntimeStates,
        )
        if instance.state != States.OK or instance.runtime_state not in (
            RuntimeStates.SHUTOFF,
            RuntimeStates.ACTIVE,
        ):
            raise serializers.ValidationError(
                _(
                    "Volume can be attached only to shutoff or active instance in OK state."
                )
            )
        volume = self.instance
        if (
            instance.service_settings != volume.service_settings
            or instance.project != volume.project
        ):
            raise serializers.ValidationError(
                _("Volume and instance should belong to the same service and project.")
            )
        if volume.availability_zone and instance.availability_zone:
            valid_zones = get_valid_availability_zones(volume)
            if (
                valid_zones
                and valid_zones.get(instance.availability_zone.name)
                != volume.availability_zone.name
            ):
                raise serializers.ValidationError(
                    _(
                        "Volume cannot be attached to virtual machine related to the other availability zone."
                    )
                )
        return instance


class OpenStackVolumeRetypeSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = models.Volume
        fields = ["type"]

    type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=False,
        required=True,
    )

    def validate_type(self, volume_type: models.VolumeType):
        volume: models.Volume = self.instance
        if volume_type == volume.type:
            raise serializers.ValidationError(_("Volume already has requested type."))
        if not is_volume_type_valid_for_tenant(volume_type, volume.tenant):
            raise serializers.ValidationError(
                _("Volume type is not visible in tenant.")
            )
        return volume_type

    @transaction.atomic
    def update(self, instance: models.Volume, validated_data):
        old_type = instance.type
        new_type: models.VolumeType = validated_data.get("type")

        if old_type:
            instance.tenant.add_quota_usage(
                volume_type_name_to_quota_name(old_type.name),
                -1 * instance.size / 1024,
                validate=True,
            )
        if new_type:
            instance.tenant.add_quota_usage(
                volume_type_name_to_quota_name(new_type.name),
                instance.size / 1024,
                validate=True,
            )

        return super().update(instance, validated_data)


class OpenStackSnapshotRestorationSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    name = serializers.CharField(write_only=True, help_text=_("New volume name."))
    description = serializers.CharField(
        required=False, help_text=_("New volume description.")
    )
    volume_state = serializers.CharField(
        read_only=True, source="volume.get_state_display"
    )

    class Meta:
        model = models.SnapshotRestoration
        fields = (
            "uuid",
            "created",
            "name",
            "description",
            "volume",
            "volume_name",
            "volume_state",
            "volume_runtime_state",
            "volume_size",
            "volume_device",
        )
        read_only_fields = ("uuid", "created", "volume")
        related_paths = {"volume": ("name", "state", "runtime_state", "size", "device")}
        extra_kwargs = dict(
            volume={
                "lookup_field": "uuid",
                "view_name": "openstack-volume-detail",
            },
        )

    @transaction.atomic
    def create(self, validated_data):
        snapshot = self.context["view"].get_object()
        validated_data["snapshot"] = snapshot
        description = (
            validated_data.pop("description", None)
            or "Restored from snapshot %s" % snapshot.name
        )

        volume = models.Volume(
            source_snapshot=snapshot,
            service_settings=snapshot.service_settings,
            tenant=snapshot.tenant,
            project=snapshot.project,
            name=validated_data.pop("name"),
            description=description,
            size=snapshot.size,
        )

        if snapshot.source_volume:
            volume.type = snapshot.source_volume.type

        volume.save()
        volume.increase_backend_quotas_usage(validate=True)
        validated_data["volume"] = volume

        return super().create(validated_data)


class OpenStackSnapshotBackupSerializer(serializers.Serializer):
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)


class OpenStackSnapshotSerializer(structure_serializers.BaseResourceActionSerializer):
    source_volume_name = serializers.ReadOnlyField(source="source_volume.name")
    source_volume_marketplace_uuid = serializers.UUIDField(
        read_only=True, source="source_volume.marketplace_uuid"
    )
    action_details = serializers.JSONField(read_only=True)
    metadata = serializers.JSONField(required=False)
    restorations = OpenStackSnapshotRestorationSerializer(many=True, read_only=True)
    backups = OpenStackSnapshotBackupSerializer(many=True, read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Snapshot
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "source_volume",
            "size",
            "metadata",
            "runtime_state",
            "source_volume_name",
            "source_volume_marketplace_uuid",
            "action",
            "action_details",
            "restorations",
            "backups",
            "kept_until",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "size",
                "source_volume",
                "metadata",
                "runtime_state",
                "action",
                "service_settings",
                "project",
            )
        )
        extra_kwargs = dict(
            source_volume={
                "lookup_field": "uuid",
                "view_name": "openstack-volume-detail",
            },
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs,
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs["source_volume"] = source_volume = self.context["view"].get_object()
        attrs["service_settings"] = source_volume.service_settings
        attrs["tenant"] = source_volume.tenant
        attrs["project"] = source_volume.project
        attrs["size"] = source_volume.size
        return super().validate(attrs)


class OpenStackNestedVolumeSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
    structure_serializers.BasicResourceSerializer,
):
    state = serializers.CharField(read_only=True, source="get_state_display")
    type_name = serializers.CharField(source="type.name", read_only=True)

    class Meta:
        model = models.Volume
        fields = (
            "url",
            "uuid",
            "name",
            "image_name",
            "state",
            "bootable",
            "size",
            "device",
            "resource_type",
            "type",
            "type_name",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "type": {
                "lookup_field": "uuid",
                "view_name": "openstack-volume-type-detail",
            },
        }


class NestedSecurityGroupRuleSerializer(BaseSecurityGroupRuleSerializer):
    class Meta(BaseSecurityGroupRuleSerializer.Meta):
        model = models.SecurityGroupRule
        fields = BaseSecurityGroupRuleSerializer.Meta.fields + ("id",)

    def to_internal_value(self, data):
        # Return exist security group as internal value if id is provided
        if "id" in data:
            try:
                return models.SecurityGroupRule.objects.get(id=data["id"])
            except models.SecurityGroup.DoesNotExist:
                raise serializers.ValidationError(
                    _("Security group with id %s does not exist") % data["id"]
                )
        else:
            internal_data = super().to_internal_value(data)
            return models.SecurityGroupRule(**internal_data)


class OpenStackNestedSecurityGroupSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    rules = NestedSecurityGroupRuleSerializer(
        many=True,
        read_only=True,
    )
    state = serializers.CharField(read_only=True, source="get_state_display")

    class Meta:
        model = models.SecurityGroup
        fields = ("url", "name", "rules", "description", "state")
        read_only_fields = ("name", "rules", "description", "state")
        extra_kwargs = {"url": {"lookup_field": "uuid"}}


class OpenStackSecurityGroupHyperlinkSerializer(serializers.Serializer):
    url = serializers.HyperlinkedRelatedField(
        queryset=models.SecurityGroup.objects.all(),
        view_name="openstack-sgp-detail",
        lookup_field="uuid",
    )

    def to_internal_value(self, data):
        return super().to_internal_value(data)["url"]


class OpenStackServerGroupHyperlinkSerializer(serializers.Serializer):
    url = serializers.HyperlinkedRelatedField(
        queryset=models.ServerGroup.objects.all(),
        view_name="openstack-server-group-detail",
        lookup_field="uuid",
    )

    def to_internal_value(self, data):
        return super().to_internal_value(data)["url"]


class OpenStackNestedServerGroupSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
):
    state = serializers.CharField(read_only=True, source="get_state_display")

    class Meta:
        model = models.ServerGroup
        fields = ("url", "name", "policy", "state")
        read_only_fields = ("name", "policy", "state")
        extra_kwargs = {"url": {"lookup_field": "uuid"}}


def _validate_instance_ports(ports, tenant, instance=None):
    """- make sure that ports belong to specified setting;
    - make sure that ports does not connect to the same subnet twice;
    - make sure that referenced existing ports are attachable to the instance.
    """
    if not ports:
        return
    subnets = [port.subnet for port in ports]
    tenants_ids = list(
        models.NetworkRBACPolicy.objects.filter(target_tenant=tenant).values_list(
            "network__tenant", flat=True
        )
    )
    tenants_ids.append(tenant.id)

    for subnet in subnets:
        if subnet.tenant.id not in tenants_ids:
            message = (
                _("Subnet %s does not belong to the same tenant as instance.") % subnet
            )
            raise serializers.ValidationError({"ports": message})

    instance_pk = instance.pk if instance is not None else None
    for port in ports:
        if not port.pk:
            continue
        if port.tenant_id not in tenants_ids:
            raise serializers.ValidationError(
                {
                    "ports": _(
                        "Port %s does not belong to the same tenant as instance."
                    )
                    % port
                }
            )
        same_instance = instance_pk is not None and port.instance_id == instance_pk
        if port.instance_id and not same_instance:
            raise serializers.ValidationError(
                {"ports": _("Port %s is already attached to another instance.") % port}
            )
        # Skip device_owner check for ports already attached to the same instance —
        # OpenStack assigns device_owner="compute:nova" to attached VM ports, and we
        # want to allow re-referencing them in update_ports.
        if port.device_owner and not same_instance:
            raise serializers.ValidationError(
                {
                    "ports": _(
                        "Port %(port)s cannot be attached because it is owned by %(owner)s."
                    )
                    % {"port": port, "owner": port.device_owner}
                }
            )

    pairs = [(port.subnet, port.backend_id) for port in ports]
    duplicates = [
        subnet for subnet, count in collections.Counter(pairs).items() if count > 1
    ]
    if duplicates:
        raise serializers.ValidationError(
            _("It is impossible to connect to subnet %s twice.") % duplicates[0][0]
        )


def _validate_instance_security_groups(security_groups, tenant):
    """Make sure that security_group belong to specific tenant."""
    for security_group in security_groups:
        if security_group.tenant != tenant:
            error = _(
                "Security group %s does not belong to the same tenant as instance."
            )
            raise serializers.ValidationError(
                {"security_groups": error % security_group.name}
            )


def _validate_instance_server_group(server_group, tenant):
    """Make sure that server_group belong to specified tenant."""

    if server_group and server_group.tenant != tenant:
        error = _("Server group %s does not belong to the same tenant as instance.")
        raise serializers.ValidationError({"server_group": error % server_group.name})


def _validate_instance_floating_ips(
    floating_ips_with_subnets: FloatingIPSpec, tenant: models.Tenant, instance_subnets
):
    if (
        floating_ips_with_subnets
        and "external_network_id" not in tenant.service_settings.options
    ):
        raise serializers.ValidationError(
            gettext(
                "Please specify tenant external network to perform floating IP operations."
            )
        )

    for floating_ip, subnet in floating_ips_with_subnets:
        if not subnet.is_connected:
            message = gettext("SubNet %s is not connected to router.") % subnet
            raise serializers.ValidationError({"floating_ips": message})
        if subnet not in instance_subnets:
            message = gettext("SubNet %s is not connected to instance.") % subnet
            raise serializers.ValidationError({"floating_ips": message})
        if not floating_ip:
            continue
        if floating_ip.state == CoreStates.CREATION_SCHEDULED:
            message = gettext(
                "Floating IP %s is already booked for another instance creation"
            )
            raise serializers.ValidationError({"floating_ips": message % floating_ip})
        if floating_ip.tenant != tenant:
            message = gettext(
                "Floating IP %s does not belong to the same tenant as instance."
            )
            raise serializers.ValidationError({"floating_ips": message % floating_ip})

    subnets = [subnet for _, subnet in floating_ips_with_subnets]
    duplicates = [
        subnet for subnet, count in collections.Counter(subnets).items() if count > 1
    ]
    if duplicates:
        raise serializers.ValidationError(
            gettext("It is impossible to use subnet %s twice.") % duplicates[0]
        )


def _validate_instance_name(data, max_len=255):
    """Copy paste from https://github.com/openstack/neutron-lib/blob/master/neutron_lib/api/validators/dns.py#L23"""

    # allow data to be lowercase. Internally OpenStack allows more flexibility
    # with hostnames as sanitizing happens, but we are more strict and want to preserve name <-> hostname mapping
    # https://github.com/openstack/nova/blob/e80300ac20388890539a7f709e526a0a5ba8e63d/nova/utils.py#L388

    DNS_LABEL_REGEX = "^([a-zA-Z0-9-]{1,63})$"
    try:
        # A trailing period is allowed to indicate that a name is fully
        # qualified per RFC 1034 (page 7).
        trimmed = data[:-1] if data.endswith(".") else data
        if len(trimmed) > max_len:
            raise TypeError(
                _("'%(trimmed)s' exceeds the %(maxlen)s character FQDN limit")
                % {"trimmed": trimmed, "maxlen": max_len}
            )
        labels = trimmed.split(".")
        for label in labels:
            if not label:
                raise TypeError(_("Encountered an empty component"))
            if label.endswith("-") or label.startswith("-"):
                raise TypeError(
                    _("Name '%s' must not start or end with a hyphen") % label
                )
            if not re.match(DNS_LABEL_REGEX, label):
                raise TypeError(
                    _(
                        "Name '%s' must be 1-63 characters long, each of "
                        "which can only be alphanumeric or a hyphen"
                    )
                    % label
                )
        # RFC 1123 hints that a TLD can't be all numeric. last is a TLD if
        # it's an FQDN.
        if len(labels) > 1 and re.match("^[0-9]+$", labels[-1]):
            raise TypeError(_("TLD '%s' must not be all numeric") % labels[-1])
    except TypeError as e:
        msg = _("'%(data)s' not a valid PQDN or FQDN. Reason: %(reason)s") % {
            "data": data,
            "reason": e,
        }
        raise serializers.ValidationError({"name": msg})


def _connect_floating_ip_to_instance(
    floating_ip: models.FloatingIP | None,
    subnet: models.SubNet,
    instance: models.Instance,
):
    """Connect floating IP to instance via specified subnet.
    If floating IP is not defined - take existing free one or create a new one.
    """
    external_network_id = instance.service_settings.options.get("external_network_id")
    if not core_utils.is_uuid_like(external_network_id):
        raise serializers.ValidationError(
            gettext("Service provider does not have valid value of external_network_id")
        )

    if not floating_ip:
        floating_ip = (
            models.FloatingIP.objects.filter(
                port__isnull=True,
                tenant=subnet.tenant,
                backend_network_id=external_network_id,
            )
            .exclude(backend_id="")
            .first()
        )
        if not floating_ip:
            floating_ip = models.FloatingIP(
                tenant=subnet.tenant,
                backend_network_id=external_network_id,
                service_settings=subnet.service_settings,
                project=subnet.project,
            )
            floating_ip.increase_backend_quotas_usage(validate=True)
    if floating_ip.backend_id:
        floating_ip.state = CoreStates.UPDATE_SCHEDULED
    floating_ip.port = models.Port.objects.filter(
        instance=instance, subnet=subnet
    ).first()
    floating_ip.save()
    return floating_ip


class OpenStackInstanceAvailabilityZoneSerializer(BaseAvailabilityZoneSerializer):
    class Meta(BaseAvailabilityZoneSerializer.Meta):
        model = models.InstanceAvailabilityZone


class OpenStackDataVolumeSerializer(serializers.Serializer):
    size = serializers.IntegerField()
    volume_type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        required=False,
    )


class OpenStackInstanceSerializer(structure_serializers.VirtualMachineSerializer):
    service_settings = serializers.HyperlinkedRelatedField(
        read_only=True,
        view_name="servicesettings-detail",
        lookup_field="uuid",
        help_text=_("OpenStack provider settings"),
    )

    tenant = serializers.HyperlinkedRelatedField(
        view_name="openstack-tenant-detail",
        lookup_field="uuid",
        queryset=models.Tenant.objects.all(),
        help_text=_("The OpenStack tenant to create the instance in"),
    )

    security_groups = OpenStackNestedSecurityGroupSerializer(many=True, required=False)
    server_group = OpenStackNestedServerGroupSerializer()
    ports = OpenStackNestedPortSerializer(many=True, required=True)
    floating_ips = OpenStackNestedFloatingIPSerializer(many=True)

    volumes = OpenStackNestedVolumeSerializer(
        many=True,
        required=False,
        read_only=True,
        help_text=_("List of volumes attached to the instance"),
    )
    action_details = serializers.JSONField(
        read_only=True, help_text=_("Details about ongoing or completed actions")
    )

    availability_zone_name = serializers.CharField(
        source="availability_zone.name",
        read_only=True,
        help_text=_("Name of the availability zone where instance is located"),
    )
    tenant_uuid = serializers.UUIDField(
        read_only=True,
        source="tenant.uuid",
        help_text=_("UUID of the OpenStack tenant"),
    )

    class Meta(structure_serializers.VirtualMachineSerializer.Meta):
        model = models.Instance
        fields = structure_serializers.VirtualMachineSerializer.Meta.fields + (
            "flavor_disk",
            "flavor_name",
            "volumes",
            "security_groups",
            "server_group",
            "floating_ips",
            "ports",
            "availability_zone",
            "availability_zone_name",
            "connect_directly_to_external_network",
            "config_drive",
            "runtime_state",
            "action",
            "action_details",
            "tenant_uuid",
            "hypervisor_hostname",
            "tenant",
            "external_address",
        )
        protected_fields = (
            structure_serializers.VirtualMachineSerializer.Meta.protected_fields
            + (
                "floating_ips",
                "security_groups",
                "server_group",
                "ports",
                "availability_zone",
                "connect_directly_to_external_network",
                "config_drive",
                "tenant",
            )
        )
        read_only_fields = (
            structure_serializers.VirtualMachineSerializer.Meta.read_only_fields
            + (
                "flavor_disk",
                "runtime_state",
                "flavor_name",
                "action",
                "hypervisor_hostname",
                "service_settings",
            )
        )
        extra_kwargs = dict(
            availability_zone={
                "lookup_field": "uuid",
                "view_name": "openstack-instance-availability-zone-detail",
            },
            **structure_serializers.VirtualMachineSerializer.Meta.extra_kwargs,
        )

    def get_fields(self):
        fields = super().get_fields()

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        user = self.context["request"].user

        if user.is_authenticated and not user.is_staff and not user.is_support:
            if "hypervisor_hostname" in fields:
                del fields["hypervisor_hostname"]

        return fields

    @staticmethod
    def eager_load(queryset, request):
        queryset = structure_serializers.VirtualMachineSerializer.eager_load(
            queryset, request
        )
        return queryset.prefetch_related(
            "security_groups",
            "security_groups__rules",
            "volumes",
        )

    def validate_name(self, name):
        _validate_instance_name(name)
        return name


class OpenStackInstanceCreateSerializer(OpenStackInstanceSerializer):
    class Meta(OpenStackInstanceSerializer.Meta):
        fields = OpenStackInstanceSerializer.Meta.fields + (
            "image",
            "flavor",
            "system_volume_size",
            "system_volume_type",
            "data_volume_size",
            "data_volume_type",
            "data_volumes",
        )

    server_group = OpenStackServerGroupHyperlinkSerializer(
        required=False,
        write_only=True,
        help_text=_("Server group for instance scheduling policy"),
    )
    ports = OpenStackCreateInstancePortSerializer(
        many=True,
        required=True,
        help_text=_("Network ports to attach to the instance"),
        write_only=True,
    )
    security_groups = OpenStackSecurityGroupHyperlinkSerializer(
        many=True,
        required=False,
        help_text=_("List of security groups to apply to the instance"),
        write_only=True,
    )
    floating_ips = OpenStackCreateFloatingIPSerializer(
        many=True,
        required=False,
        help_text=_("Floating IPs to assign to the instance"),
        write_only=True,
    )
    flavor = serializers.HyperlinkedRelatedField(
        view_name="openstack-flavor-detail",
        lookup_field="uuid",
        queryset=models.Flavor.objects.all().select_related("settings"),
        write_only=True,
        help_text=_("The flavor to use for the instance"),
    )

    image = serializers.HyperlinkedRelatedField(
        view_name="openstack-image-detail",
        lookup_field="uuid",
        # Exclude rescue-tagged images: an image with hw_rescue_device or
        # hw_rescue_bus is meant for Nova rescue mode and is typically an
        # ISO that won't boot a usable system disk. The HyperlinkedRelatedField
        # will report it as not-found if a client tries to pass one.
        queryset=models.Image.objects.filter(
            hw_rescue_device="", hw_rescue_bus=""
        ).select_related("settings"),
        write_only=True,
        help_text=_("The OS image to use for the instance"),
    )
    system_volume_size = serializers.IntegerField(
        min_value=1024,
        write_only=True,
        help_text=_(
            "Size of the system volume in MiB. Minimum size is 1024 MiB (1 GiB)"
        ),
    )
    system_volume_type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        required=False,
        write_only=True,
        help_text=_("Volume type for the system volume"),
    )
    data_volume_size = serializers.IntegerField(
        min_value=1024,
        required=False,
        write_only=True,
        help_text=_("Size of the data volume in MiB. Minimum size is 1024 MiB (1 GiB)"),
    )
    data_volume_type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        required=False,
        write_only=True,
        help_text=_("Volume type for the data volume"),
    )
    data_volumes = OpenStackDataVolumeSerializer(
        many=True,
        required=False,
        write_only=True,
        help_text=_("Additional data volumes to attach to the instance"),
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # skip validation on object update
        if self.instance is not None:
            return attrs

        tenant: models.Tenant = attrs["tenant"]
        flavor: models.Flavor = attrs["flavor"]
        image: models.Image = attrs["image"]
        system_volume_type: models.VolumeType | None = attrs.get("system_volume_type")
        data_volume_type: models.VolumeType | None = attrs.get("data_volume_type")

        if not is_flavor_valid_for_tenant(flavor, tenant):
            raise serializers.ValidationError(_("Flavor is not visible in tenant."))

        if not is_image_valid_for_tenant(image, tenant):
            raise serializers.ValidationError(_("Image is not visible in tenant."))

        if system_volume_type and not is_volume_type_valid_for_tenant(
            system_volume_type, tenant
        ):
            raise serializers.ValidationError(
                _("System volume type is not visible in tenant.")
            )

        if data_volume_type and not is_volume_type_valid_for_tenant(
            data_volume_type, tenant
        ):
            raise serializers.ValidationError(
                _("Data volume type is not visible in tenant.")
            )

        if image.min_ram > flavor.ram:
            raise serializers.ValidationError(
                {
                    "flavor": _("RAM of flavor is not enough for selected image %s")
                    % image.min_ram
                }
            )

        if image.min_disk > attrs["system_volume_size"]:
            raise serializers.ValidationError(
                {
                    "system_volume_size": _(
                        "System volume size has to be greater than %s MiB"
                    )
                    % image.min_disk
                }
            )
        if (
            attrs.get("connect_directly_to_external_network", False)
            and "external_network_id" not in tenant.service_settings.options
        ):
            raise serializers.ValidationError(
                gettext(
                    "Please specify tenant external network to request direct connection to external network."
                )
            )

        ports = attrs.get("ports", [])
        if len(ports) == 0:
            raise serializers.ValidationError(
                gettext("Please specify at least one network.")
            )

        security_groups = attrs.get("security_groups", [])
        has_port_security_disabled = any(
            not port.port_security_enabled for port in ports
        )
        if has_port_security_disabled and security_groups:
            raise serializers.ValidationError(
                _("Security groups cannot be assigned when port security is disabled.")
            )

        _validate_instance_security_groups(security_groups, tenant)
        _validate_instance_server_group(attrs.get("server_group"), tenant)
        _validate_instance_ports(ports, tenant)
        subnets = [port.subnet for port in ports]
        floating_ips = cast(FloatingIPSpec, attrs.get("floating_ips", []))
        _validate_instance_floating_ips(floating_ips, tenant, subnets)

        availability_zone: models.InstanceAvailabilityZone | None = attrs.get(
            "availability_zone"
        )
        if availability_zone and availability_zone.tenant != tenant:
            raise serializers.ValidationError(
                _(
                    "Instance and availability zone must belong to the same tenant as instance."
                )
            )
        if availability_zone and not availability_zone.available:
            raise serializers.ValidationError(_("Zone is not available."))

        if (
            not availability_zone
            and settings.WALDUR_OPENSTACK["REQUIRE_AVAILABILITY_ZONE"]
        ):
            if (
                models.InstanceAvailabilityZone.objects.filter(tenant=tenant).count()
                > 0
            ):
                raise serializers.ValidationError(_("Availability zone is mandatory."))

        self.validate_quotas(attrs)
        return attrs

    def validate_quotas(self, attrs):
        parts: list[SharedQuotaMixin] = []

        tenant: models.Tenant = attrs["tenant"]
        flavor: models.Flavor = attrs["flavor"]
        system_volume_size = attrs["system_volume_size"]
        data_volume_size = attrs.get("data_volume_size", 0)
        data_volumes = attrs.get("data_volumes", [])

        instance = models.Instance(cores=flavor.cores, ram=flavor.ram)
        parts.append(instance)

        system_volume = models.Volume(
            size=system_volume_size,
            type=attrs.get("system_volume_type"),
        )
        parts.append(system_volume)

        if data_volume_size:
            data_volume = models.Volume(
                size=data_volume_size,
                type=attrs.get("data_volume_type"),
            )
            parts.append(data_volume)

        for volume in data_volumes:
            data_volume = models.Volume(
                size=volume["size"],
                type=volume.get("volume_type"),
            )
            parts.append(data_volume)

        quota_deltas = {}
        for part in parts:
            for quota, delta in part.get_quota_deltas().items():
                quota_deltas.setdefault(quota, 0)
                quota_deltas[quota] += delta

        tenant.validate_quota_change(quota_deltas)

    def _find_volume_availability_zone(self, instance: models.Instance):
        # Find volume AZ using instance AZ. It is assumed that user can't select arbitrary
        # combination of volume and instance AZ. Once instance AZ is selected,
        # volume AZ is taken from settings.

        volume_availability_zone = None
        valid_zones = get_valid_availability_zones(instance)
        if instance.availability_zone and valid_zones:
            volume_availability_zone_name = valid_zones.get(
                instance.availability_zone.name
            )
            if volume_availability_zone_name:
                try:
                    volume_availability_zone = (
                        models.VolumeAvailabilityZone.objects.get(
                            name=volume_availability_zone_name,
                            settings=instance.service_settings,
                            available=True,
                        )
                    )
                except models.VolumeAvailabilityZone.DoesNotExist:
                    pass
        return volume_availability_zone

    @transaction.atomic
    def create(self, validated_data):
        """Store flavor, ssh_key and image details into instance model.
        Create volumes and security groups for instance.
        """
        security_groups = validated_data.pop("security_groups", [])
        server_group = validated_data.get("server_group")
        ports = validated_data.pop("ports", [])
        floating_ips_with_subnets = cast(
            FloatingIPSpec, validated_data.pop("floating_ips", [])
        )
        tenant: models.Tenant = validated_data["tenant"]
        service_settings = tenant.service_settings
        validated_data["service_settings"] = service_settings
        project = validated_data["project"]
        ssh_key: core_models.SshPublicKey = validated_data.get("ssh_public_key")
        if ssh_key:
            # We want names to be human readable in backend.
            # OpenStack only allows latin letters, digits, dashes, underscores and spaces
            # as key names, thus we mangle the original name.
            safe_name = re.sub(r"[^-a-zA-Z0-9 _]+", "_", ssh_key.name)[:17]
            validated_data["key_name"] = f"{ssh_key.uuid.hex}-{safe_name}"
            validated_data["key_fingerprint"] = ssh_key.fingerprint_md5

        flavor: models.Flavor = validated_data["flavor"]
        validated_data["flavor_name"] = flavor.name
        validated_data["cores"] = flavor.cores
        validated_data["ram"] = flavor.ram
        validated_data["flavor_disk"] = flavor.disk

        image: models.Image = validated_data["image"]
        validated_data["image_name"] = image.name
        validated_data["min_disk"] = image.min_disk
        validated_data["min_ram"] = image.min_ram

        system_volume_size = validated_data["system_volume_size"]
        data_volume_size = validated_data.get("data_volume_size", 0)
        total_disk = data_volume_size + system_volume_size

        data_volumes = validated_data.get("data_volumes", [])
        if data_volumes:
            total_disk += sum(volume["size"] for volume in data_volumes)

        validated_data["disk"] = total_disk

        instance = super().create(validated_data)

        instance.security_groups.add(*security_groups)
        instance.server_group = server_group
        for port in ports:
            port.backend_id = None
            port.instance = instance
            # For shared networks: always assign port to instance's tenant
            # This ensures ports belong to the instance tenant, not the network owner
            try:
                # Check if network is shared (different from instance tenant)
                if port.network.tenant != tenant:
                    port.tenant = tenant
                    port.project = project
            except Exception:
                # If there's any issue accessing port.tenant, set it to instance tenant
                port.tenant = tenant
                port.project = project
            port.save()
        for floating_ip, subnet in floating_ips_with_subnets:
            _connect_floating_ip_to_instance(floating_ip, subnet, instance)

        volume_availability_zone = self._find_volume_availability_zone(instance)

        # volumes
        volumes: list[models.Volume] = []
        system_volume = models.Volume.objects.create(
            name=f"{instance.name[:143]}-system",  # volume name cannot be longer than 150 symbols
            service_settings=service_settings,
            tenant=tenant,
            project=project,
            size=system_volume_size,
            image=image,
            image_name=image.name,
            bootable=True,
            availability_zone=volume_availability_zone,
            type=validated_data.get("system_volume_type"),
        )
        volumes.append(system_volume)

        if data_volume_size:
            data_volume = models.Volume.objects.create(
                name=f"{instance.name[:145]}-data",  # volume name cannot be longer than 150 symbols
                service_settings=service_settings,
                tenant=tenant,
                project=project,
                size=data_volume_size,
                availability_zone=volume_availability_zone,
                type=validated_data.get("data_volume_type"),
            )
            volumes.append(data_volume)

        for index, volume in enumerate(data_volumes):
            data_volume = models.Volume.objects.create(
                name=f"{instance.name[:140]}-data-{index + 2}",  # volume name cannot be longer than 150 symbols
                service_settings=service_settings,
                tenant=tenant,
                project=project,
                size=volume["size"],
                availability_zone=volume_availability_zone,
                type=volume.get("volume_type"),
            )
            volumes.append(data_volume)

        for volume in volumes:
            volume.increase_backend_quotas_usage(validate=True)

        instance.volumes.add(*volumes)
        return instance


class InstanceRescueSerializer(serializers.Serializer):
    """Input serializer for the rescue action.

    For volume-backed instances, both an explicit `rescue_image` and the
    "stable device rescue" Glance properties are required — Nova will leave
    a BFV instance in an unrecoverable ERROR state otherwise (per
    https://specs.openstack.org/openstack/nova-specs/specs/ussuri/implemented/virt-bfv-instance-rescue.html).
    """

    rescue_image = serializers.HyperlinkedRelatedField(
        view_name="openstack-image-detail",
        lookup_field="uuid",
        queryset=models.Image.objects.all(),
        required=False,
        allow_null=True,
        help_text=_(
            "Optional rescue image. Required for volume-backed instances; "
            "must be a Glance image with hw_rescue_device or hw_rescue_bus "
            "set (a 'stable device rescue' image)."
        ),
    )

    def validate(self, attrs):
        instance: models.Instance = self.instance
        rescue_image: models.Image | None = attrs.get("rescue_image")

        # Cross-tenant: rescue image must be visible to the instance's tenant.
        if rescue_image is not None:
            tenant: models.Tenant = instance.tenant
            if not rescue_image.tenants.filter(pk=tenant.pk).exists():
                raise serializers.ValidationError(
                    {
                        "rescue_image": _(
                            "Rescue image is not visible to the instance's tenant."
                        )
                    }
                )

        # Volume-backed instance safety: BFV rescue requires an explicit
        # stable-device rescue image.
        is_volume_backed = instance.volumes.filter(bootable=True).exists()
        if is_volume_backed:
            if rescue_image is None:
                raise serializers.ValidationError(
                    {
                        "rescue_image": _(
                            "Volume-backed instances require an explicit rescue image."
                        )
                    }
                )
            if not rescue_image.is_rescue_image:
                raise serializers.ValidationError(
                    {
                        "rescue_image": _(
                            "Selected image is not a stable-device rescue image. "
                            "Volume-backed instances require an image tagged with "
                            "hw_rescue_device or hw_rescue_bus, otherwise the rescue "
                            "will fail and leave the instance in an unrecoverable state."
                        )
                    }
                )

        return attrs


class InstanceFlavorChangeSerializer(serializers.Serializer):
    flavor = serializers.HyperlinkedRelatedField(
        view_name="openstack-flavor-detail",
        lookup_field="uuid",
        queryset=models.Flavor.objects.all(),
        help_text=_(
            "The new flavor to use for the instance. Flavor change can only be done when instance is stopped."
        ),
    )

    def validate_flavor(self, flavor: models.Flavor):
        if flavor is not None:
            if flavor.name == self.instance.flavor_name:
                raise serializers.ValidationError(
                    _("New flavor is the same as current.")
                )

            tenant: models.Tenant = self.instance.tenant

            if not is_flavor_valid_for_tenant(flavor, tenant):
                raise serializers.ValidationError(
                    _("New flavor is not visible in tenant.")
                )

        return flavor

    @transaction.atomic
    def update(self, instance: models.Instance, validated_data):
        flavor = validated_data.get("flavor")

        # We should update tenant quotas.
        # Otherwise stale quotas would be used for quota validation during instance provisioning.
        # Note that all tenant quotas are injected to service settings when application is bootstrapped.

        for quota_holder in (instance.service_settings, instance.tenant):
            quota_holder.add_quota_usage(
                "ram", flavor.ram - instance.ram, validate=True
            )
            quota_holder.add_quota_usage(
                "vcpu", flavor.cores - instance.cores, validate=True
            )

        instance.ram = flavor.ram
        instance.cores = flavor.cores
        instance.flavor_disk = flavor.disk
        instance.flavor_name = flavor.name
        instance.save(update_fields=["ram", "cores", "flavor_name", "flavor_disk"])
        return instance


class OpenStackInstanceSecurityGroupsUpdateSerializer(serializers.Serializer):
    security_groups = serializers.HyperlinkedRelatedField(
        many=True,
        view_name="openstack-sgp-detail",
        lookup_field="uuid",
        queryset=models.SecurityGroup.objects.all(),
        help_text=_("List of security groups to be assigned to the instance."),
    )

    def validate_security_groups(self, security_groups):
        for security_group in security_groups:
            if security_group.tenant != self.instance.tenant:
                raise serializers.ValidationError(
                    _("Security group %s is not within the same tenant")
                    % security_group.name
                )

        return security_groups

    @transaction.atomic
    def update(self, instance, validated_data):
        security_groups = validated_data.pop("security_groups", None)
        if security_groups is not None:
            instance.security_groups.clear()
            instance.security_groups.add(*security_groups)

        return instance


class OpenStackInstanceAllowedAddressPairsUpdateSerializer(serializers.Serializer):
    subnet = serializers.HyperlinkedRelatedField(
        queryset=models.SubNet.objects.all(),
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
        write_only=True,
        help_text=_("The subnet to update allowed address pairs for."),
    )

    allowed_address_pairs = OpenStackAllowedAddressPairField(
        help_text=_(
            "List of allowed address pairs to set on the port. Each pair should contain 'ip_address' and optional 'mac_address'."
        )
    )

    @transaction.atomic
    def update(self, instance, validated_data):
        subnet = validated_data["subnet"]
        try:
            port = models.Port.objects.get(instance=instance, subnet=subnet)
        except models.Port.DoesNotExist:
            raise serializers.ValidationError(
                _('Instance is not connected to subnet "%s" yet.') % subnet
            )

        port.allowed_address_pairs = validated_data["allowed_address_pairs"]
        port.save(update_fields=["allowed_address_pairs"])
        return instance


class OpenStackInstancePortsUpdateSerializer(serializers.Serializer):
    ports = OpenStackCreatePortSerializer(many=True)

    def validate_ports(self, ports):
        _validate_instance_ports(ports, self.instance.tenant, instance=self.instance)
        return ports

    @transaction.atomic
    def update(self, instance, validated_data):
        ports = validated_data["ports"]
        new_subnets = [ip.subnet for ip in ports]
        # delete stale ports
        models.Port.objects.filter(instance=instance, network__isnull=False).exclude(
            subnet__in=new_subnets
        ).delete()
        # create or attach ports
        for port in ports:
            if port.pk:
                # Existing port returned by serializer — attach it to the instance
                port.instance = instance
                port.save(update_fields=["instance"])
            else:
                match = models.Port.objects.filter(
                    instance=instance, subnet=port.subnet
                ).first()
                if not match:
                    models.Port.objects.create(
                        instance=instance,
                        subnet=port.subnet,
                        network=port.subnet.network,
                        tenant=port.subnet.tenant,
                        project=port.subnet.project,
                        service_settings=port.subnet.service_settings,
                        fixed_ips=port.fixed_ips or [],
                    )

        return instance


class OpenStackInstanceFloatingIPsUpdateSerializer(serializers.Serializer):
    floating_ips = OpenStackCreateFloatingIPSerializer(many=True, required=False)

    def validate(self, attrs):
        subnets = self.instance.subnets.all()
        floating_ips = cast(FloatingIPSpec, attrs.get("floating_ips", []))
        _validate_instance_floating_ips(floating_ips, self.instance.tenant, subnets)
        return attrs

    def update(self, instance, validated_data):
        floating_ips_with_subnets = cast(
            FloatingIPSpec, validated_data.get("floating_ips")
        )
        floating_ips_to_disconnect = list(self.instance.floating_ips)

        # Store both old and new floating IP addresses for action event logger
        new_floating_ips = [
            floating_ip
            for (floating_ip, subnet) in floating_ips_with_subnets
            if floating_ip
        ]
        instance._old_floating_ips = [
            floating_ip.address for floating_ip in floating_ips_to_disconnect
        ]
        instance._new_floating_ips = [
            floating_ip.address for floating_ip in new_floating_ips
        ]

        for floating_ip, subnet in floating_ips_with_subnets:
            if floating_ip in floating_ips_to_disconnect:
                floating_ips_to_disconnect.remove(floating_ip)
                continue
            _connect_floating_ip_to_instance(floating_ip, subnet, instance)
        for floating_ip in floating_ips_to_disconnect:
            floating_ip.port = None
            floating_ip.save()
        return instance


class OpenStackBackupRestorationSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(
        required=False,
        help_text=_("New instance name. Leave blank to use source instance name."),
    )
    security_groups = OpenStackNestedSecurityGroupSerializer(
        many=True, source="instance.security_groups"
    )
    ports = OpenStackNestedPortSerializer(many=True, source="instance.ports")
    floating_ips = OpenStackNestedFloatingIPSerializer(
        many=True, source="instance.floating_ips"
    )

    class Meta:
        model = models.BackupRestoration
        fields = (
            "uuid",
            "instance",
            "created",
            "flavor",
            "name",
            "floating_ips",
            "security_groups",
            "ports",
        )
        read_only_fields = ("url", "uuid", "instance", "created", "backup")
        extra_kwargs = dict(
            instance={
                "lookup_field": "uuid",
                "view_name": "openstack-instance-detail",
            },
            flavor={
                "lookup_field": "uuid",
                "view_name": "openstack-flavor-detail",
                "allow_null": False,
                "required": True,
            },
        )


class OpenStackBackupRestorationCreateSerializer(OpenStackBackupRestorationSerializer):
    security_groups = OpenStackSecurityGroupHyperlinkSerializer(
        many=True,
        required=False,
        help_text=_("Security groups that will be assigned to the restored instance"),
    )
    ports = OpenStackCreatePortSerializer(
        many=True,
        required=False,
        help_text=_("Network ports that will be attached to the restored instance"),
    )
    floating_ips = OpenStackCreateFloatingIPSerializer(
        many=True,
        required=False,
        help_text=_("Floating IPs that will be assigned to the restored instance"),
    )

    def validate(self, attrs):
        flavor = attrs["flavor"]
        backup: models.Backup = self.context["view"].get_object()
        bootable_volumes_count = backup.instance.volumes.filter(bootable=True).count()
        if bootable_volumes_count == 0:
            raise serializers.ValidationError(
                _("OpenStack instance should have bootable volume.")
            )
        elif bootable_volumes_count > 1:
            raise serializers.ValidationError(
                _(
                    "OpenStack instance should have exactly one bootable volume, found {}."
                ).format(bootable_volumes_count)
            )

        tenant = backup.instance.tenant

        if not is_flavor_valid_for_tenant(flavor, tenant):
            raise serializers.ValidationError(
                {"flavor": _("Flavor is not visible in tenant.")}
            )

        _validate_instance_security_groups(attrs.get("security_groups", []), tenant)

        ports = attrs.get("ports", [])
        _validate_instance_ports(ports, tenant)

        subnets = [port.subnet for port in ports]
        floating_ips = cast(FloatingIPSpec, attrs.get("floating_ips", []))
        _validate_instance_floating_ips(floating_ips, backup.tenant, subnets)

        return attrs

    @transaction.atomic
    def update(self, backup_instance: models.Backup, validated_data):
        flavor: models.Flavor = validated_data["flavor"]
        validated_data["backup"] = backup = backup_instance
        source_instance = backup.instance
        # instance that will be restored
        metadata = backup.metadata or {}
        instance = models.Instance.objects.create(
            name=validated_data.pop("name", None)
            or metadata.get("name", source_instance.name),
            description=metadata.get("description", ""),
            service_settings=backup.service_settings,
            tenant=backup.tenant,
            project=backup.project,
            flavor_disk=flavor.disk,
            flavor_name=flavor.name,
            key_name=source_instance.key_name,
            key_fingerprint=source_instance.key_fingerprint,
            cores=flavor.cores,
            ram=flavor.ram,
            min_ram=metadata.get("min_ram", 0),
            min_disk=metadata.get("min_disk", 0),
            image_name=metadata.get("image_name", ""),
            user_data=metadata.get("user_data", ""),
            disk=sum([snapshot.size for snapshot in backup.snapshots.all()]),
        )

        instance.ports.add(*validated_data.pop("ports", []), bulk=False)
        instance.security_groups.add(*validated_data.pop("security_groups", []))

        for floating_ip, subnet in cast(
            FloatingIPSpec, validated_data.pop("floating_ips", [])
        ):
            _connect_floating_ip_to_instance(floating_ip, subnet, instance)

        instance.increase_backend_quotas_usage(validate=True)
        validated_data["instance"] = instance
        backup_restoration = super().create(validated_data)
        # restoration for each instance volume from snapshot.
        for snapshot in backup.snapshots.all():
            volume = models.Volume(
                source_snapshot=snapshot,
                service_settings=snapshot.service_settings,
                tenant=snapshot.tenant,
                project=snapshot.project,
                name=f"{instance.name[:143]}-volume",
                description="Restored from backup %s" % backup.uuid.hex,
                size=snapshot.size,
            )
            volume.save()
            volume.increase_backend_quotas_usage(validate=True)
            instance.volumes.add(volume)
        return backup_restoration


class OpenStackBackupSerializer(structure_serializers.BaseResourceActionSerializer):
    metadata = serializers.JSONField(read_only=True)
    instance_name = serializers.ReadOnlyField(source="instance.name")
    instance_marketplace_uuid = serializers.UUIDField(
        read_only=True, source="instance.marketplace_uuid"
    )
    instance_security_groups = OpenStackNestedSecurityGroupSerializer(
        read_only=True, many=True, source="instance.security_groups"
    )
    instance_ports = OpenStackNestedPortSerializer(
        read_only=True, many=True, source="instance.ports"
    )
    instance_floating_ips = OpenStackNestedFloatingIPSerializer(
        read_only=True, many=True, source="instance.floating_ips"
    )

    restorations = OpenStackBackupRestorationSerializer(many=True, read_only=True)
    tenant_uuid = serializers.UUIDField(read_only=True, source="tenant.uuid")

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Backup
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "kept_until",
            "metadata",
            "instance",
            "instance_name",
            "instance_marketplace_uuid",
            "restorations",
            "instance_security_groups",
            "instance_ports",
            "instance_floating_ips",
            "tenant_uuid",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "instance",
                "service_settings",
                "project",
            )
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "instance": {
                "lookup_field": "uuid",
                "view_name": "openstack-instance-detail",
            },
        }

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs["instance"] = instance = self.context["view"].get_object()
        attrs["service_settings"] = instance.service_settings
        attrs["tenant"] = instance.tenant
        attrs["project"] = instance.project
        attrs["metadata"] = self.get_backup_metadata(instance)
        return super().validate(attrs)

    @transaction.atomic
    def create(self, validated_data):
        backup = super().create(validated_data)
        self.create_backup_snapshots(backup)
        return backup

    @staticmethod
    def get_backup_metadata(instance):
        return {
            "name": instance.name,
            "description": instance.description,
            "min_ram": instance.min_ram,
            "min_disk": instance.min_disk,
            "size": instance.size,
            "key_name": instance.key_name,
            "key_fingerprint": instance.key_fingerprint,
            "user_data": instance.user_data,
            "flavor_name": instance.flavor_name,
            "image_name": instance.image_name,
        }

    @staticmethod
    def create_backup_snapshots(backup):
        for volume in backup.instance.volumes.all():
            snapshot = models.Snapshot.objects.create(
                name=f"Part of backup: {backup.name[:60]} (volume: {volume.name[:60]})",
                service_settings=backup.service_settings,
                tenant=backup.tenant,
                project=backup.project,
                size=volume.size,
                source_volume=volume,
                description=f"Part of backup {backup.name} (UUID: {backup.uuid.hex})",
            )
            snapshot.increase_backend_quotas_usage(validate=True)
            backup.snapshots.add(snapshot)


def get_instance(openstack_floating_ip) -> models.Instance | None:
    # cache openstack instance on openstack floating_ip instance
    if hasattr(openstack_floating_ip, "_instance"):
        return openstack_floating_ip._instance
    if not openstack_floating_ip.backend_id or not openstack_floating_ip.address:
        openstack_floating_ip._instance = None
        return
    try:
        floating_ip = models.FloatingIP.objects.exclude(port__isnull=True).get(
            backend_id=openstack_floating_ip.backend_id,
            address=openstack_floating_ip.address,
        )
    except models.FloatingIP.DoesNotExist:
        openstack_floating_ip._instance = None
    else:
        instance = getattr(floating_ip.port, "instance", None)
        openstack_floating_ip._instance = instance
        return instance


def get_instance_attr(openstack_floating_ip: models.FloatingIP, name) -> str | None:
    instance = get_instance(openstack_floating_ip)
    return getattr(instance, name, None)


def get_instance_uuid(
    serializer, openstack_floating_ip: models.FloatingIP
) -> str | None:
    return get_instance_attr(openstack_floating_ip, "uuid")


def get_instance_name(
    serializer, openstack_floating_ip: models.FloatingIP
) -> str | None:
    return get_instance_attr(openstack_floating_ip, "name")


def get_instance_url(
    serializer, openstack_floating_ip: models.FloatingIP
) -> str | None:
    instance = get_instance(openstack_floating_ip)
    if instance:
        return reverse(
            "openstack-instance-detail",
            kwargs={"uuid": instance.uuid.hex},
            request=serializer.context["request"],
        )


def add_instance_fields(sender, fields, **kwargs):
    """Add instance-related fields to the serializer."""
    fields["instance_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_instance_uuid", get_instance_uuid)
    fields["instance_name"] = serializers.SerializerMethodField()
    setattr(sender, "get_instance_name", get_instance_name)
    fields["instance_url"] = serializers.SerializerMethodField()
    setattr(sender, "get_instance_url", get_instance_url)


core_signals.pre_serializer_fields.connect(
    add_instance_fields, sender=OpenStackFloatingIPSerializer
)


class OpenStackConsoleLogSerializer(serializers.Serializer):
    length = serializers.IntegerField(required=False)


class OpenStackBackendInstanceSerializer(serializers.ModelSerializer):
    availability_zone = serializers.ReadOnlyField(source="availability_zone.name")
    state = serializers.CharField(read_only=True, source="get_state_display")

    class Meta:
        model = models.Instance
        fields = (
            "name",
            "key_name",
            "start_time",
            "state",
            "runtime_state",
            "created",
            "backend_id",
            "availability_zone",
            "hypervisor_hostname",
        )


class OpenStackBackendVolumesSerializer(serializers.ModelSerializer):
    availability_zone = serializers.ReadOnlyField(source="availability_zone.name")
    state = serializers.CharField(read_only=True, source="get_state_display")
    type = serializers.ReadOnlyField(source="type.name")

    class Meta:
        model = models.Volume
        fields = (
            "name",
            "description",
            "size",
            "metadata",
            "backend_id",
            "type",
            "bootable",
            "runtime_state",
            "state",
            "availability_zone",
        )


class OpenStackInstanceFloatingIpsSerializer(serializers.ListSerializer):
    child = OpenStackNestedFloatingIPSerializer(read_only=True)


class OpenStackPortIPUpdateSerializer(serializers.Serializer):
    subnet = serializers.HyperlinkedRelatedField(
        queryset=models.SubNet.objects.all(),
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
        write_only=True,
        help_text=_("The subnet where the new IP address will be allocated"),
    )
    ip_address = serializers.IPAddressField(
        help_text=_("The IP address to assign within the subnet"),
    )

    def validate(self, attrs):
        subnet = attrs.get("subnet")
        ip = attrs.get("ip_address")

        port = self.context.get("port")
        if port and subnet.network_id != port.network_id:
            raise serializers.ValidationError(
                {"subnet": "Subnet does not belong to the same network as the port."}
            )

        if subnet.allocation_pools:
            ip_addr = ip_address(ip)
            in_pool = False
            for pool in subnet.allocation_pools:
                start_ip = ip_address(pool["start"])
                end_ip = ip_address(pool["end"])
                if start_ip <= ip_addr and ip_addr <= end_ip:
                    in_pool = True
                    break
            if not in_pool:
                raise serializers.ValidationError(
                    {"ip_address": "IP address is outside of allocation pools."}
                )
        return attrs


class OpenStackRouterInterfaceSerializer(serializers.Serializer):
    subnet = serializers.HyperlinkedRelatedField(
        queryset=models.SubNet.objects.all(),
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
        required=False,
        help_text=_(
            "The subnet to connect to the router. Either subnet or port must be specified, but not both."
        ),
    )
    port = serializers.HyperlinkedRelatedField(
        queryset=models.Port.objects.all(),
        view_name="openstack-port-detail",
        lookup_field="uuid",
        required=False,
        help_text=_(
            "The port to connect to the router. Either subnet or port must be specified, but not both."
        ),
    )

    def validate(self, attrs):
        if not attrs.get("subnet") and not attrs.get("port"):
            raise serializers.ValidationError("Either subnet or port must be provided.")
        if attrs.get("subnet") and attrs.get("port"):
            raise serializers.ValidationError(
                "Only one of subnet or port can be provided."
            )
        if isinstance(self.context, dict) and "view" in self.context:
            view = self.context["view"]
            router: models.Router = view.get_object()
            tenant = router.tenant
            if attrs.get("subnet"):
                subnet: models.SubNet = attrs["subnet"]
                if subnet.tenant != tenant:
                    raise serializers.ValidationError(
                        "Subnet must belong to the same tenant as the router."
                    )
            if attrs.get("port"):
                port: models.Port = attrs["port"]

                if port.tenant != tenant:
                    raise serializers.ValidationError(
                        "Port must belong to the same tenant as the router."
                    )

        return attrs
