import collections
import copy
import logging
import re
from ipaddress import AddressValueError, IPv4Network, NetmaskValueError

import pytz
from django.conf import settings
from django.contrib.auth import password_validation
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import validate_ipv46_address
from django.db import transaction
from django.db.models import Q
from django.template.defaultfilters import slugify
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from iptools.ipv4 import validate_cidr as is_valid_ipv4_cidr
from iptools.ipv6 import validate_cidr as is_valid_ipv6_cidr
from netaddr import AddrFormatError, IPNetwork, all_matching_cidrs
from rest_framework import serializers
from rest_framework.reverse import reverse

from waldur_core.core import models as core_models
from waldur_core.core import serializers as core_serializers
from waldur_core.core import signals as core_signals
from waldur_core.core import utils as core_utils
from waldur_core.core.validators import BackendURLValidator, validate_x509_certificate
from waldur_core.quotas.models import SharedQuotaMixin
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_openstack.utils import (
    get_valid_availability_zones,
    is_flavor_valid_for_tenant,
    is_image_valid_for_tenant,
    is_volume_type_valid_for_tenant,
    volume_type_name_to_quota_name,
)

from . import models

logger = logging.getLogger(__name__)


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


class FlavorSerializer(structure_serializers.BasePropertySerializer):
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

    def get_display_name(self, flavor: models.Flavor):
        return f"{flavor.name} ({flavor.cores} CPU, {flavor.ram} MB RAM, {flavor.disk} MB HDD)"


class ImageSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Image
        fields = ("url", "uuid", "name", "min_disk", "min_ram", "settings")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "settings": {"lookup_field": "uuid"},
        }


class VolumeTypeSerializer(structure_serializers.BasePropertySerializer):
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


class TenantQuotaSerializer(serializers.Serializer):
    instances = serializers.IntegerField(min_value=1, required=False)
    volumes = serializers.IntegerField(min_value=1, required=False)
    snapshots = serializers.IntegerField(min_value=1, required=False)
    ram = serializers.IntegerField(min_value=1, required=False)
    vcpu = serializers.IntegerField(min_value=1, required=False)
    storage = serializers.IntegerField(min_value=1, required=False)
    security_group_count = serializers.IntegerField(min_value=1, required=False)
    security_group_rule_count = serializers.IntegerField(min_value=1, required=False)


class FloatingIPSerializer(structure_serializers.BaseResourceActionSerializer):
    port = serializers.HyperlinkedRelatedField(
        view_name="openstack-port-detail",
        lookup_field="uuid",
        read_only=True,
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
        return super().validate(attrs)


class FloatingIPAttachSerializer(serializers.Serializer):
    port = serializers.HyperlinkedRelatedField(
        queryset=models.Port.objects.all(),
        view_name="openstack-port-detail",
        lookup_field="uuid",
        many=False,
        required=True,
    )


class FloatingIPDescriptionUpdateSerializer(serializers.Serializer):
    description = serializers.CharField(
        required=False, help_text=_("New floating IP description.")
    )


class BaseSecurityGroupRuleSerializer(serializers.ModelSerializer):
    remote_group_name = serializers.ReadOnlyField(source="remote_group.name")
    remote_group_uuid = serializers.ReadOnlyField(source="remote_group.uuid")

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


class SecurityGroupRuleSerializer(
    BaseSecurityGroupRuleSerializer, serializers.HyperlinkedModelSerializer
):
    class Meta(BaseSecurityGroupRuleSerializer.Meta):
        model = models.SecurityGroupRule
        fields = BaseSecurityGroupRuleSerializer.Meta.fields + ("id", "remote_group")
        extra_kwargs = dict(
            remote_group={"lookup_field": "uuid", "view_name": "openstack-sgp-detail"},
        )

    def validate(self, rule):
        """
        Please note that validate function accepts rule object instead of validated data
        because it is used as a child of list serializer.
        """
        ethertype = rule.ethertype
        protocol = rule.protocol
        from_port = rule.from_port
        to_port = rule.to_port
        cidr = rule.cidr
        remote_group = rule.remote_group

        if cidr:
            if ethertype == models.SecurityGroupRule.IPv4 and not is_valid_ipv4_cidr(
                cidr
            ):
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
                _(
                    "You can specify either the remote_group_id or cidr attribute, not both."
                )
            )

        if to_port is None:
            raise serializers.ValidationError(
                {"to_port": _("Empty value is not allowed.")}
            )

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
                    {
                        "to_port": _("Value should be in range [-1, 255], found %d")
                        % to_port
                    }
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

        return rule


class SecurityGroupRuleCreateSerializer(SecurityGroupRuleSerializer):
    """Create rules on security group creation"""

    def to_internal_value(self, data):
        if "id" in data:
            raise serializers.ValidationError(
                _("Cannot add existed rule with id %s to new security group")
                % data["id"]
            )
        internal_data = super(SecurityGroupRuleSerializer, self).to_internal_value(data)
        return models.SecurityGroupRule(**internal_data)


class SecurityGroupRuleUpdateSerializer(SecurityGroupRuleSerializer):
    def to_internal_value(self, data):
        """Create new rule if id is not specified, update exist rule if id is specified"""
        security_group = self.context["view"].get_object()
        internal_data = super(SecurityGroupRuleSerializer, self).to_internal_value(data)
        if "id" not in data:
            return models.SecurityGroupRule(
                security_group=security_group, **internal_data
            )
        rule_id = data.pop("id")
        try:
            rule = security_group.rules.get(id=rule_id)
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


class SecurityGroupRuleListUpdateSerializer(serializers.ListSerializer):
    child = SecurityGroupRuleUpdateSerializer()

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


class SecurityGroupSerializer(structure_serializers.BaseResourceActionSerializer):
    rules = SecurityGroupRuleCreateSerializer(many=True)

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
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + ("rules",)
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


class SecurityGroupUpdateSerializer(serializers.ModelSerializer):
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


class CreateServerGroupSerializer(structure_serializers.BaseResourceActionSerializer):
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
        }

    display_name = serializers.SerializerMethodField()
    instances = serializers.SerializerMethodField()

    def get_display_name(self, server_group):
        return f"Name: {server_group.name}, Policy: {server_group.policy}"

    def get_instances(self, server_group):
        filtered_instances = models.Instance.objects.filter(
            server_group__backend_id=server_group.backend_id
        ).values("backend_id", "name", "uuid")
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


class TenantSerializer(structure_serializers.BaseResourceSerializer):
    quotas = serializers.ReadOnlyField()
    subnet_cidr = serializers.CharField(
        default="192.168.42.0/24",
        initial="192.168.42.0/24",
        write_only=True,
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Tenant
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "availability_zone",
            "internal_network_id",
            "external_network_id",
            "user_username",
            "user_password",
            "quotas",
            "subnet_cidr",
            "default_volume_type_name",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "internal_network_id",
                "external_network_id",
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

    def validate_subnet_cidr(self, value):
        return validate_private_subnet_cidr(value)

    def get_fields(self):
        fields = super().get_fields()
        if not settings.WALDUR_OPENSTACK["TENANT_CREDENTIALS_VISIBLE"]:
            for field in ("user_username", "user_password", "access_url"):
                if field in fields:
                    del fields[field]

        return fields

    def _validate_service_settings(self, service_settings, project):
        """Administrator can create tenant only using not shared service settings"""
        user = self.context["request"].user
        message = _(
            "You do not have permissions to create tenant in this project using selected service."
        )
        if service_settings.shared and not user.is_staff:
            raise serializers.ValidationError(message)
        if not service_settings.shared and not structure_permissions._has_admin_access(
            user, project
        ):
            raise serializers.ValidationError(message)

    def validate_security_groups_configuration(self):
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

        if not self.instance:
            self._validate_service_settings(attrs["service_settings"], attrs["project"])

        self.validate_security_groups_configuration()

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
        service_settings = validated_data["service_settings"]
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
        with transaction.atomic():
            tenant = super().create(validated_data)
            network = models.Network.objects.create(
                name=slugified_name + "-int-net",
                description=_("Internal network for tenant %s") % tenant.name,
                tenant=tenant,
                service_settings=tenant.service_settings,
                project=tenant.project,
            )
            models.SubNet.objects.create(
                name=slugified_name + "-sub-net",
                description=_("SubNet for tenant %s internal network") % tenant.name,
                network=network,
                tenant=tenant,
                service_settings=tenant.service_settings,
                project=tenant.project,
                cidr=subnet_cidr,
                allocation_pools=_generate_subnet_allocation_pool(subnet_cidr),
                dns_nameservers=service_settings.options.get("dns_nameservers", []),
            )

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

        return tenant


class _NestedSubNetSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.SubNet
        fields = (
            "name",
            "description",
            "cidr",
            "gateway_ip",
            "allocation_pools",
            "ip_version",
            "enable_dhcp",
        )


class StaticRouteSerializer(serializers.Serializer):
    destination = serializers.CharField()
    nexthop = serializers.IPAddressField()

    def validate_destination(self, value):
        try:
            return str(IPNetwork(value))
        except (AddrFormatError, TypeError):
            raise serializers.ValidationError("Invalid CIDR format.")


class RouterSetRoutesSerializer(serializers.Serializer):
    routes = StaticRouteSerializer(many=True)

    def validate(self, attrs):
        fixed_ips = self.instance.fixed_ips
        for route in attrs["routes"]:
            nexthop = route["nexthop"]
            if nexthop in fixed_ips:
                raise serializers.ValidationError(
                    _("Nexthop %s is used by router.") % nexthop
                )
        return attrs


class RouterSerializer(structure_serializers.BaseResourceSerializer):
    routes = StaticRouteSerializer(many=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    tenant_uuid = serializers.CharField(source="tenant.uuid", read_only=True)
    fixed_ips = serializers.JSONField(read_only=True)

    class Meta:
        model = models.Router
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "routes",
            "fixed_ips",
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "openstack-router-detail"},
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
        )


class NestedSecurityGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.SecurityGroup
        fields = ("uuid", "name")


class PortSerializer(structure_serializers.BaseResourceActionSerializer):
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    tenant_uuid = serializers.CharField(source="tenant.uuid", read_only=True)
    network_name = serializers.CharField(source="network.name", read_only=True)
    network_uuid = serializers.CharField(source="network.uuid", read_only=True)
    allowed_address_pairs = serializers.JSONField(read_only=True)
    floating_ips = serializers.HyperlinkedRelatedField(
        view_name="openstack-fip-detail",
        lookup_field="uuid",
        read_only=True,
        many=True,
    )
    fixed_ips = serializers.JSONField(required=False)
    security_groups = NestedSecurityGroupSerializer(many=True, read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Port
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "fixed_ips",
            "mac_address",
            "allowed_address_pairs",
            "tenant",
            "tenant_name",
            "tenant_uuid",
            "network",
            "network_name",
            "network_uuid",
            "floating_ips",
            "device_id",
            "device_owner",
            "port_security_enabled",
            "security_groups",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "tenant",
                "allowed_address_pairs",
                "service_settings",
                "project",
                "device_id",
                "device_owner",
                "port_security_enabled",
                "security_groups",
            )
        )
        extra_kwargs = dict(
            url={"lookup_field": "uuid", "view_name": "openstack-port-detail"},
            tenant={"lookup_field": "uuid", "view_name": "openstack-tenant-detail"},
            network={"lookup_field": "uuid", "view_name": "openstack-network-detail"},
        )

    def validate(self, attrs):
        if self.instance:
            return attrs
        fixed_ips = attrs.get("fixed_ips")
        network: models.Network = self.context["view"].get_object()
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

                if fixed_ip.get("subnet_id") == "":
                    raise serializers.ValidationError(
                        _("subnet_id field must not be blank. Got %(fixed_ip)s.")
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
        attrs["tenant"] = network.tenant

        return super().validate(attrs)


class NetworkSerializer(
    structure_serializers.FieldFilteringMixin,
    structure_serializers.BaseResourceActionSerializer,
):
    subnets = _NestedSubNetSerializer(many=True, read_only=True)
    tenant_name = serializers.CharField(source="tenant.name", read_only=True)
    tenant_uuid = serializers.CharField(source="tenant.uuid", read_only=True)

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
            ("segmentation_id", lambda user: user.is_staff or user.is_support),
        ]


class SetMtuSerializer(serializers.Serializer):
    mtu = serializers.IntegerField()

    def update(self, network, validated_data):
        network.mtu = validated_data["mtu"]
        network.save(update_fields=["mtu"])
        return network


class SubNetSerializer(structure_serializers.BaseResourceActionSerializer):
    cidr = serializers.CharField(
        required=False,
        initial="192.168.42.0/24",
        label="CIDR",
    )
    allocation_pools = serializers.JSONField(read_only=True)
    network_name = serializers.CharField(source="network.name", read_only=True)
    tenant = serializers.HyperlinkedRelatedField(
        source="network.tenant",
        view_name="openstack-tenant-detail",
        read_only=True,
        lookup_field="uuid",
    )
    tenant_name = serializers.CharField(source="network.tenant.name", read_only=True)
    dns_nameservers = serializers.JSONField(required=False)
    host_routes = StaticRouteSerializer(many=True, required=False)

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
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + ("cidr",)
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

    def validate_cidr(self, value):
        if value:
            return validate_private_subnet_cidr(value)

    def validate(self, attrs):
        if attrs.get("disable_gateway") and attrs.get("gateway_ip"):
            raise serializers.ValidationError(
                _(
                    "These parameters are mutually exclusive: disable_gateway and gateway_ip."
                )
            )

        if self.instance is None:
            attrs["network"] = network = self.context["view"].get_object()
            attrs["tenant"] = network.tenant
            if network.subnets.count() >= 1:
                raise serializers.ValidationError(
                    _("Internal network cannot have more than one subnet.")
                )
            if "cidr" not in attrs:
                attrs["cidr"] = "192.168.42.0/24"
            cidr = attrs["cidr"]
            if models.SubNet.objects.filter(
                cidr=cidr, network__tenant=network.tenant
            ).exists():
                raise serializers.ValidationError(
                    _('Subnet with cidr "%s" is already registered') % cidr
                )

            attrs["service_settings"] = network.service_settings
            attrs["project"] = network.project
            options = network.service_settings.options
            attrs["allocation_pools"] = _generate_subnet_allocation_pool(cidr)
            attrs.setdefault("dns_nameservers", options.get("dns_nameservers", []))
            self.check_cidr_overlap(network.tenant, cidr)

        return attrs

    def check_cidr_overlap(self, tenant, new_cidr):
        cidr_list = list(
            models.SubNet.objects.filter(network__tenant=tenant).values_list(
                "cidr", flat=True
            )
        )
        for old_cidr in cidr_list:
            old_ipnet = IPNetwork(old_cidr)
            new_ipnet = IPNetwork(new_cidr)
            if all_matching_cidrs(new_ipnet, [old_cidr]) or all_matching_cidrs(
                old_ipnet, [new_cidr]
            ):
                raise serializers.ValidationError(
                    _("CIDR %(new_cidr)s overlaps with CIDR %(old_cidr)s")
                    % dict(new_cidr=new_cidr, old_cidr=old_cidr)
                )

    def update(self, instance, validated_data):
        host_routes = validated_data.pop("host_routes", [])
        instance = super().update(instance, validated_data)
        instance.host_routes = host_routes
        instance.save()
        return instance


def _generate_subnet_allocation_pool(cidr):
    first_octet, second_octet, third_octet, _ = cidr.split(".", 3)
    subnet_settings = settings.WALDUR_OPENSTACK["SUBNET"]
    format_data = {
        "first_octet": first_octet,
        "second_octet": second_octet,
        "third_octet": third_octet,
    }
    return [
        {
            "start": subnet_settings["ALLOCATION_POOL_START"].format(**format_data),
            "end": subnet_settings["ALLOCATION_POOL_END"].format(**format_data),
        }
    ]


class TenantChangePasswordSerializer(serializers.Serializer):
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


class NestedPortSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    allowed_address_pairs = serializers.JSONField(read_only=True)
    fixed_ips = serializers.JSONField(read_only=True)

    class Meta:
        model = models.Port
        fields = (
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
        )
        read_only_fields = (
            "fixed_ips",
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
            "subnet": {
                "lookup_field": "uuid",
                "view_name": "openstack-subnet-detail",
            },
        }

    def to_internal_value(self, data):
        internal_value = super().to_internal_value(data)
        subnet: models.SubNet = internal_value["subnet"]
        return models.Port(
            subnet=subnet,
            network=subnet.network,
            tenant=subnet.tenant,
            project=subnet.project,
            service_settings=subnet.service_settings,
        )


class NestedFloatingIPSerializer(
    core_serializers.AugmentedSerializerMixin,
    core_serializers.HyperlinkedRelatedModelSerializer,
):
    subnet = serializers.HyperlinkedRelatedField(
        queryset=models.SubNet.objects.all(),
        source="port.subnet",
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
    )
    subnet_uuid = serializers.ReadOnlyField(source="port.subnet.uuid")
    subnet_name = serializers.ReadOnlyField(source="port.subnet.name")
    subnet_description = serializers.ReadOnlyField(source="port.subnet.description")
    subnet_cidr = serializers.ReadOnlyField(source="port.subnet.cidr")
    port_fixed_ips = serializers.JSONField(source="port.fixed_ips", read_only=True)

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

    def to_internal_value(self, data):
        """
        Return pair (floating_ip, subnet) as internal value.

        On floating IP creation user should specify what subnet should be used
        for connection and may specify what exactly floating IP should be used.
        If floating IP is not specified it will be represented as None.
        """
        floating_ip = None
        if "url" in data:
            # use HyperlinkedRelatedModelSerializer (parent of NestedFloatingIPSerializer)
            # method to convert "url" to FloatingIP object
            floating_ip = super().to_internal_value(data)

        # use HyperlinkedModelSerializer (parent of HyperlinkedRelatedModelSerializer)
        # to convert "subnet" to SubNet object
        internal_value = super(
            core_serializers.HyperlinkedRelatedModelSerializer, self
        ).to_internal_value(data)
        subnet = internal_value["port"]["subnet"]

        return floating_ip, subnet


class UsageStatsSerializer(serializers.Serializer):
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


class ServerGroupSerializer(structure_serializers.BasePropertySerializer):
    class Meta(structure_serializers.BasePropertySerializer.Meta):
        model = models.ServerGroup
        fields = (
            "url",
            "uuid",
            "name",
            "policy",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "settings": {"lookup_field": "uuid"},
            "server-groups": {
                "lookup_field": "uuid",
                "view_name": "openstack-server-group-detail",
            },
        }


class VolumeAvailabilityZoneSerializer(BaseAvailabilityZoneSerializer):
    class Meta(BaseAvailabilityZoneSerializer.Meta):
        model = models.VolumeAvailabilityZone


class VolumeSerializer(structure_serializers.BaseResourceSerializer):
    action_details = serializers.JSONField(read_only=True)
    metadata = serializers.JSONField(read_only=True)
    instance_name = serializers.SerializerMethodField()
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
    tenant_uuid = serializers.ReadOnlyField(source="tenant.uuid")

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
            "tenant",
            "tenant_uuid",
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

    def get_instance_name(self, volume):
        if volume.instance:
            return volume.instance.name

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
            type = attrs.get("type")
            if type and not is_volume_type_valid_for_tenant(type, tenant):
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


class VolumeExtendSerializer(serializers.Serializer):
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
            models.Instance.States,
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


class VolumeRetypeSerializer(serializers.HyperlinkedModelSerializer):
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

    def validate_type(self, type: models.VolumeType):
        volume: models.Volume = self.instance
        if type == volume.type:
            raise serializers.ValidationError(_("Volume already has requested type."))
        if not is_volume_type_valid_for_tenant(type, volume.tenant):
            raise serializers.ValidationError(
                _("Volume type is not visible in tenant.")
            )
        return type

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


class SnapshotRestorationSerializer(
    core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer
):
    name = serializers.CharField(write_only=True, help_text=_("New volume name."))
    description = serializers.CharField(
        required=False, help_text=_("New volume description.")
    )
    volume_state = serializers.ReadOnlyField(source="volume.get_state_display")

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


class SnapshotSerializer(structure_serializers.BaseResourceActionSerializer):
    source_volume_name = serializers.ReadOnlyField(source="source_volume.name")
    action_details = serializers.JSONField(read_only=True)
    metadata = serializers.JSONField(required=False)
    restorations = SnapshotRestorationSerializer(many=True, read_only=True)
    snapshot_schedule_uuid = serializers.ReadOnlyField(source="snapshot_schedule.uuid")

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Snapshot
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "source_volume",
            "size",
            "metadata",
            "runtime_state",
            "source_volume_name",
            "action",
            "action_details",
            "restorations",
            "kept_until",
            "snapshot_schedule",
            "snapshot_schedule_uuid",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "size",
                "source_volume",
                "metadata",
                "runtime_state",
                "action",
                "snapshot_schedule",
                "service_settings",
                "project",
            )
        )
        extra_kwargs = dict(
            source_volume={
                "lookup_field": "uuid",
                "view_name": "openstack-volume-detail",
            },
            snapshot_schedule={
                "lookup_field": "uuid",
                "view_name": "openstack-snapshot-schedule-detail",
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


class NestedVolumeSerializer(
    core_serializers.AugmentedSerializerMixin,
    serializers.HyperlinkedModelSerializer,
    structure_serializers.BasicResourceSerializer,
):
    state = serializers.ReadOnlyField(source="get_state_display")
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


class NestedSecurityGroupSerializer(
    core_serializers.AugmentedSerializerMixin,
    core_serializers.HyperlinkedRelatedModelSerializer,
):
    rules = NestedSecurityGroupRuleSerializer(
        many=True,
        read_only=True,
    )
    state = serializers.ReadOnlyField(source="get_state_display")

    class Meta:
        model = models.SecurityGroup
        fields = ("url", "name", "rules", "description", "state")
        read_only_fields = ("name", "rules", "description", "state")
        extra_kwargs = {"url": {"lookup_field": "uuid"}}


class NestedServerGroupSerializer(
    core_serializers.AugmentedSerializerMixin,
    core_serializers.HyperlinkedRelatedModelSerializer,
):
    state = serializers.ReadOnlyField(source="get_state_display")

    class Meta:
        model = models.ServerGroup
        fields = ("url", "name", "policy", "state")
        read_only_fields = ("name", "policy", "state")
        extra_kwargs = {"url": {"lookup_field": "uuid"}}


def _validate_instance_ports(ports, tenant):
    """- make sure that ports belong to specified setting;
    - make sure that ports does not connect to the same subnet twice;
    """
    if not ports:
        return
    subnets = [port.subnet for port in ports]
    for subnet in subnets:
        if subnet.tenant != tenant:
            message = (
                _("Subnet %s does not belong to the same tenant as instance.") % subnet
            )
            raise serializers.ValidationError({"ports": message})
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
    floating_ips_with_subnets, tenant, instance_subnets
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
        if floating_ip.state == models.FloatingIP.States.CREATION_SCHEDULED:
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
                _("'%(trimmed)s' exceeds the %(maxlen)s character FQDN " "limit")
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
    floating_ip,
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
        floating_ip.state = models.FloatingIP.States.UPDATE_SCHEDULED
    floating_ip.port = models.Port.objects.filter(
        instance=instance, subnet=subnet
    ).first()
    floating_ip.save()
    return floating_ip


class InstanceAvailabilityZoneSerializer(BaseAvailabilityZoneSerializer):
    class Meta(BaseAvailabilityZoneSerializer.Meta):
        model = models.InstanceAvailabilityZone


class DataVolumeSerializer(serializers.Serializer):
    size = serializers.IntegerField()
    volume_type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        required=False,
    )


class InstanceSerializer(structure_serializers.VirtualMachineSerializer):
    flavor = serializers.HyperlinkedRelatedField(
        view_name="openstack-flavor-detail",
        lookup_field="uuid",
        queryset=models.Flavor.objects.all().select_related("settings"),
        write_only=True,
    )

    image = serializers.HyperlinkedRelatedField(
        view_name="openstack-image-detail",
        lookup_field="uuid",
        queryset=models.Image.objects.all().select_related("settings"),
        write_only=True,
    )

    service_settings = serializers.HyperlinkedRelatedField(
        read_only=True,
        view_name="servicesettings-detail",
        lookup_field="uuid",
    )

    tenant = serializers.HyperlinkedRelatedField(
        view_name="openstack-tenant-detail",
        lookup_field="uuid",
        queryset=models.Tenant.objects.all(),
    )

    security_groups = NestedSecurityGroupSerializer(
        queryset=models.SecurityGroup.objects.all(), many=True, required=False
    )
    server_group = NestedServerGroupSerializer(
        queryset=models.ServerGroup.objects.all(), required=False
    )
    ports = NestedPortSerializer(many=True, required=True)
    floating_ips = NestedFloatingIPSerializer(
        queryset=models.FloatingIP.objects.all().filter(port__isnull=True),
        many=True,
        required=False,
    )

    system_volume_size = serializers.IntegerField(min_value=1024, write_only=True)
    system_volume_type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        required=False,
        write_only=True,
    )
    data_volume_size = serializers.IntegerField(
        min_value=1024, required=False, write_only=True
    )
    data_volume_type = serializers.HyperlinkedRelatedField(
        view_name="openstack-volume-type-detail",
        queryset=models.VolumeType.objects.all(),
        lookup_field="uuid",
        allow_null=True,
        required=False,
        write_only=True,
    )
    data_volumes = DataVolumeSerializer(many=True, required=False, write_only=True)
    volumes = NestedVolumeSerializer(many=True, required=False, read_only=True)
    action_details = serializers.JSONField(read_only=True)

    availability_zone_name = serializers.CharField(
        source="availability_zone.name", read_only=True
    )
    tenant_uuid = serializers.ReadOnlyField(source="tenant.uuid")

    class Meta(structure_serializers.VirtualMachineSerializer.Meta):
        model = models.Instance
        fields = structure_serializers.VirtualMachineSerializer.Meta.fields + (
            "image",
            "flavor",
            "flavor_disk",
            "flavor_name",
            "system_volume_size",
            "system_volume_type",
            "data_volume_size",
            "data_volume_type",
            "volumes",
            "data_volumes",
            "security_groups",
            "server_group",
            "floating_ips",
            "ports",
            "availability_zone",
            "availability_zone_name",
            "connect_directly_to_external_network",
            "runtime_state",
            "action",
            "action_details",
            "tenant_uuid",
            "hypervisor_hostname",
            "tenant",
        )
        protected_fields = (
            structure_serializers.VirtualMachineSerializer.Meta.protected_fields
            + (
                "flavor",
                "image",
                "system_volume_size",
                "data_volume_size",
                "floating_ips",
                "security_groups",
                "server_group",
                "ports",
                "availability_zone",
                "connect_directly_to_external_network",
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
        user = self.context["request"].user

        if not user.is_staff and not user.is_support:
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

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # skip validation on object update
        if self.instance is not None:
            return attrs

        tenant: models.Tenant = attrs["tenant"]
        flavor: models.Flavor = attrs["flavor"]
        image: models.Image = attrs["image"]
        system_volume_type: models.VolumeType = attrs.get("system_volume_type")
        data_volume_type: models.VolumeType = attrs.get("data_volume_type")

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

        _validate_instance_security_groups(attrs.get("security_groups", []), tenant)
        _validate_instance_server_group(attrs.get("server_group", None), tenant)
        _validate_instance_ports(ports, tenant)
        subnets = [port.subnet for port in ports]
        _validate_instance_floating_ips(attrs.get("floating_ips", []), tenant, subnets)

        availability_zone: models.InstanceAvailabilityZone = attrs.get(
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
                            tenant=instance.tenant,
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
        floating_ips_with_subnets = validated_data.pop("floating_ips", [])
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
            port.instance = instance
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


class InstanceFlavorChangeSerializer(serializers.Serializer):
    flavor = serializers.HyperlinkedRelatedField(
        view_name="openstack-flavor-detail",
        lookup_field="uuid",
        queryset=models.Flavor.objects.all(),
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


class InstanceDeleteSerializer(serializers.Serializer):
    delete_volumes = serializers.BooleanField(default=True)
    release_floating_ips = serializers.BooleanField(
        label=_("Release floating IPs"), default=True
    )

    def validate(self, attrs):
        if (
            attrs["delete_volumes"]
            and models.Snapshot.objects.filter(
                source_volume__instance=self.instance
            ).exists()
        ):
            raise serializers.ValidationError(
                _("Cannot delete instance. One of its volumes has attached snapshot.")
            )
        return attrs


class InstanceSecurityGroupsUpdateSerializer(serializers.Serializer):
    security_groups = NestedSecurityGroupSerializer(
        queryset=models.SecurityGroup.objects.all(),
        many=True,
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


class AllowedAddressPairSerializer(serializers.Serializer):
    ip_address = serializers.CharField(
        default="192.168.42.0/24",
        initial="192.168.42.0/24",
        write_only=True,
    )
    mac_address = serializers.CharField(required=False)

    def validate_ip_address(self, value):
        return validate_private_cidr(value)


class InstanceAllowedAddressPairsUpdateSerializer(serializers.Serializer):
    subnet = serializers.HyperlinkedRelatedField(
        queryset=models.SubNet.objects.all(),
        view_name="openstack-subnet-detail",
        lookup_field="uuid",
        write_only=True,
    )

    allowed_address_pairs = AllowedAddressPairSerializer(many=True)

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


class InstancePortsUpdateSerializer(serializers.Serializer):
    ports = NestedPortSerializer(many=True)

    def validate_ports(self, ports):
        _validate_instance_ports(ports, self.instance.tenant)
        return ports

    @transaction.atomic
    def update(self, instance, validated_data):
        ports = validated_data["ports"]
        new_subnets = [ip.subnet for ip in ports]
        # delete stale ports
        models.Port.objects.filter(instance=instance).exclude(
            subnet__in=new_subnets
        ).delete()
        # create new ports
        for port in ports:
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
                )

        return instance


class InstanceFloatingIPsUpdateSerializer(serializers.Serializer):
    floating_ips = NestedFloatingIPSerializer(
        queryset=models.FloatingIP.objects.all(), many=True, required=False
    )

    def get_fields(self):
        fields = super().get_fields()
        instance = self.instance
        if instance:
            queryset = models.FloatingIP.objects.all().filter(
                Q(port__isnull=True) | Q(port__instance=instance)
            )
            fields["floating_ips"] = NestedFloatingIPSerializer(
                queryset=queryset, many=True, required=False
            )
            fields["floating_ips"].view_name = "openstack-fip-detail"
        return fields

    def validate(self, attrs):
        subnets = self.instance.subnets.all()
        _validate_instance_floating_ips(
            attrs["floating_ips"], self.instance.tenant, subnets
        )
        return attrs

    def update(self, instance, validated_data):
        floating_ips_with_subnets = validated_data["floating_ips"]
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


class BackupRestorationSerializer(serializers.HyperlinkedModelSerializer):
    name = serializers.CharField(
        required=False,
        help_text=_("New instance name. Leave blank to use source instance name."),
    )
    security_groups = NestedSecurityGroupSerializer(
        queryset=models.SecurityGroup.objects.all(), many=True, required=False
    )
    ports = NestedPortSerializer(many=True, required=False)
    floating_ips = NestedFloatingIPSerializer(
        queryset=models.FloatingIP.objects.all().filter(port__isnull=True),
        many=True,
        required=False,
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

    def validate(self, attrs):
        flavor = attrs["flavor"]
        backup: models.Backup = self.context["view"].get_object()
        try:
            backup.instance.volumes.get(bootable=True)
        except ObjectDoesNotExist:
            raise serializers.ValidationError(
                _("OpenStack instance should have bootable volume.")
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
        _validate_instance_floating_ips(
            attrs.get("floating_ips", []), backup.tenant, subnets
        )

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

        for floating_ip, subnet in validated_data.pop("floating_ips", []):
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


class BackupSerializer(structure_serializers.BaseResourceActionSerializer):
    metadata = serializers.JSONField(read_only=True)
    instance_name = serializers.ReadOnlyField(source="instance.name")
    instance_security_groups = NestedSecurityGroupSerializer(
        read_only=True, many=True, source="instance.security_groups"
    )
    instance_ports = NestedPortSerializer(
        read_only=True, many=True, source="instance.ports"
    )
    instance_floating_ips = NestedFloatingIPSerializer(
        read_only=True, many=True, source="instance.floating_ips"
    )

    restorations = BackupRestorationSerializer(many=True, read_only=True)
    backup_schedule_uuid = serializers.ReadOnlyField(source="backup_schedule.uuid")
    tenant_uuid = serializers.ReadOnlyField(source="tenant.uuid")

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Backup
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "kept_until",
            "metadata",
            "instance",
            "instance_name",
            "restorations",
            "backup_schedule",
            "backup_schedule_uuid",
            "instance_security_groups",
            "instance_ports",
            "instance_floating_ips",
            "tenant_uuid",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "instance",
                "backup_schedule",
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
            "backup_schedule": {
                "lookup_field": "uuid",
                "view_name": "openstack-backup-schedule-detail",
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


class BaseScheduleSerializer(structure_serializers.BaseResourceActionSerializer):
    timezone = serializers.ChoiceField(
        choices=[(t, t) for t in pytz.all_timezones],
        initial=timezone.get_current_timezone_name(),
        default=timezone.get_current_timezone_name(),
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            "retention_time",
            "timezone",
            "maximal_number_of_resources",
            "schedule",
            "is_active",
            "next_trigger_at",
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                "is_active",
                "next_trigger_at",
                "service_settings",
                "project",
            )
        )


class BackupScheduleSerializer(BaseScheduleSerializer):
    class Meta(BaseScheduleSerializer.Meta):
        model = models.BackupSchedule
        fields = BaseScheduleSerializer.Meta.fields + ("instance", "instance_name")
        read_only_fields = BaseScheduleSerializer.Meta.read_only_fields + (
            "backups",
            "instance",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "instance": {
                "lookup_field": "uuid",
                "view_name": "openstack-instance-detail",
            },
        }
        related_paths = {
            "instance": ("name",),
        }

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        instance = self.context["view"].get_object()
        if not instance.volumes.filter(bootable=True).exists():
            raise serializers.ValidationError(
                _("OpenStack instance should have bootable volume.")
            )
        attrs["instance"] = instance
        attrs["service_settings"] = instance.service_settings
        attrs["tenant"] = instance.tenant
        attrs["project"] = instance.project
        attrs["state"] = instance.States.OK
        return super().validate(attrs)


class SnapshotScheduleSerializer(BaseScheduleSerializer):
    class Meta(BaseScheduleSerializer.Meta):
        model = models.SnapshotSchedule
        fields = BaseScheduleSerializer.Meta.fields + (
            "source_volume",
            "source_volume_name",
        )
        read_only_fields = BaseScheduleSerializer.Meta.read_only_fields + (
            "snapshots",
            "source_volume",
        )
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
            "source_volume": {
                "lookup_field": "uuid",
                "view_name": "openstack-volume-detail",
            },
        }
        related_paths = {
            "source_volume": ("name",),
        }

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        volume = self.context["view"].get_object()
        attrs["source_volume"] = volume
        attrs["tenant"] = volume.tenant
        attrs["service_settings"] = volume.service_settings
        attrs["project"] = volume.project
        attrs["state"] = volume.States.OK
        return super().validate(attrs)


def get_instance(openstack_floating_ip):
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


def get_instance_attr(openstack_floating_ip, name):
    instance = get_instance(openstack_floating_ip)
    return getattr(instance, name, None)


def get_instance_uuid(serializer, openstack_floating_ip):
    return get_instance_attr(openstack_floating_ip, "uuid")


def get_instance_name(serializer, openstack_floating_ip):
    return get_instance_attr(openstack_floating_ip, "name")


def get_instance_url(serializer, openstack_floating_ip):
    instance = get_instance(openstack_floating_ip)
    if instance:
        return reverse(
            "openstack-instance-detail",
            kwargs={"uuid": instance.uuid.hex},
            request=serializer.context["request"],
        )


def add_instance_fields(sender, fields, **kwargs):
    fields["instance_uuid"] = serializers.SerializerMethodField()
    setattr(sender, "get_instance_uuid", get_instance_uuid)
    fields["instance_name"] = serializers.SerializerMethodField()
    setattr(sender, "get_instance_name", get_instance_name)
    fields["instance_url"] = serializers.SerializerMethodField()
    setattr(sender, "get_instance_url", get_instance_url)


core_signals.pre_serializer_fields.connect(
    add_instance_fields, sender=FloatingIPSerializer
)


class ConsoleLogSerializer(serializers.Serializer):
    length = serializers.IntegerField(required=False)


class SharedSettingsCustomerSerializer(serializers.Serializer):
    name = serializers.ReadOnlyField()
    uuid = serializers.ReadOnlyField()
    created = serializers.ReadOnlyField()
    abbreviation = serializers.ReadOnlyField()
    vm_count = serializers.ReadOnlyField()


class BackendInstanceSerializer(serializers.ModelSerializer):
    availability_zone = serializers.ReadOnlyField(source="availability_zone.name")
    state = serializers.ReadOnlyField(source="get_state_display")

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


class BackendVolumesSerializer(serializers.ModelSerializer):
    availability_zone = serializers.ReadOnlyField(source="availability_zone.name")
    state = serializers.ReadOnlyField(source="get_state_display")
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
