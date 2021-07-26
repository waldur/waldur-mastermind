import copy
import logging
from ipaddress import AddressValueError, IPv4Network, NetmaskValueError

from django.conf import settings
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address
from django.db import transaction
from django.db.models import Q
from django.template.defaultfilters import slugify
from django.utils.translation import ugettext_lazy as _
from iptools.ipv4 import validate_cidr as is_valid_ipv4_cidr
from iptools.ipv6 import validate_cidr as is_valid_ipv6_cidr
from netaddr import IPNetwork, all_matching_cidrs
from rest_framework import serializers

from waldur_core.core import utils as core_utils
from waldur_core.quotas import serializers as quotas_serializers
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure import serializers as structure_serializers
from waldur_openstack.openstack_base.serializers import (
    BaseOpenStackServiceSerializer,
    BaseSecurityGroupRuleSerializer,
    BaseVolumeTypeSerializer,
)

from . import models

logger = logging.getLogger(__name__)


class OpenStackServiceSerializer(BaseOpenStackServiceSerializer):

    tenant_name = serializers.CharField(
        source='options.tenant_name',
        label=_('Tenant name'),
        default='admin',
        required=False,
    )

    volume_availability_zone_name = serializers.CharField(
        source='options.volume_availability_zone_name',
        label=_('Name of default volume availability zone to use'),
        help_text=_('Default availability zone name for provisioned volumes'),
        required=False,
    )

    valid_availability_zones = serializers.CharField(
        source='options.valid_availability_zones',
        help_text=_(
            'Optional dictionary where key is Nova availability '
            'zone name and value is Cinder availability zone name.'
        ),
        required=False,
    )

    external_network_id = serializers.CharField(
        source='options.external_network_id',
        help_text=_(
            'ID of OpenStack external network that will be connected to tenants'
        ),
        label=_('Public/gateway network UUID'),
        required=False,
    )

    latitude = serializers.CharField(
        source='options.latitude',
        help_text=_('Latitude of the datacenter (e.g. 40.712784)'),
        required=False,
    )

    longitude = serializers.CharField(
        source='options.longitude',
        help_text=_('Longitude of the datacenter (e.g. -74.005941)'),
        required=False,
    )

    access_url = serializers.CharField(
        source='options.access_url',
        label=_('Access URL'),
        help_text=_('Publicly accessible OpenStack dashboard URL'),
        required=False,
    )

    dns_nameservers = serializers.ListField(
        child=serializers.CharField(),
        source='options.dns_nameservers',
        help_text=_(
            'Default value for new subnets DNS name servers. Should be defined as list.'
        ),
        required=False,
    )

    create_ha_routers = serializers.BooleanField(
        source='options.create_ha_routers',
        default=False,
        help_text=_('Create highly available Neutron routers.'),
        required=False,
    )


class FlavorSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Flavor
        fields = ('url', 'uuid', 'name', 'cores', 'ram', 'disk', 'display_name')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    display_name = serializers.SerializerMethodField()

    def get_display_name(self, flavor):
        return "{} ({} CPU, {} MB RAM, {} MB HDD)".format(
            flavor.name, flavor.cores, flavor.ram, flavor.disk
        )


class ImageSerializer(structure_serializers.BasePropertySerializer):
    class Meta:
        model = models.Image
        fields = ('url', 'uuid', 'name', 'min_disk', 'min_ram')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class VolumeTypeSerializer(BaseVolumeTypeSerializer):
    class Meta(BaseVolumeTypeSerializer.Meta):
        model = models.VolumeType


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
        view_name='openstack-port-detail', lookup_field='uuid', read_only=True,
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.FloatingIP
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'runtime_state',
            'address',
            'backend_network_id',
            'tenant',
            'tenant_name',
            'tenant_uuid',
            'port',
        )
        related_paths = ('tenant',)
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                'runtime_state',
                'address',
                'description',
                'name',
                'tenant',
                'backend_network_id',
                'service_settings',
                'project',
                'port',
            )
        )
        extra_kwargs = dict(
            tenant={'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail'},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs['tenant'] = tenant = self.context['view'].get_object()
        attrs['service_settings'] = tenant.service_settings
        attrs['project'] = tenant.project
        return super(FloatingIPSerializer, self).validate(attrs)


class FloatingIPAttachSerializer(serializers.Serializer):
    port = serializers.HyperlinkedRelatedField(
        queryset=models.Port.objects.all(),
        view_name='openstack-port-detail',
        lookup_field='uuid',
        many=False,
        required=True,
    )


class SecurityGroupRuleSerializer(
    BaseSecurityGroupRuleSerializer, serializers.HyperlinkedModelSerializer
):
    class Meta(BaseSecurityGroupRuleSerializer.Meta):
        model = models.SecurityGroupRule
        fields = BaseSecurityGroupRuleSerializer.Meta.fields + ('id', 'remote_group')
        extra_kwargs = dict(
            remote_group={'lookup_field': 'uuid', 'view_name': 'openstack-sgp-detail'},
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
                        'cidr': _(
                            'Expected CIDR format: <0-255>.<0-255>.<0-255>.<0-255>/<0-32>'
                        )
                    }
                )
            elif ethertype == models.SecurityGroupRule.IPv6 and not is_valid_ipv6_cidr(
                cidr
            ):
                raise serializers.ValidationError(
                    {
                        'cidr': _(
                            'IPv6 addresses are represented as eight groups, separated by colons.'
                        )
                    }
                )

        if cidr and remote_group:
            raise serializers.ValidationError(
                _(
                    'You can specify either the remote_group_id or cidr attribute, not both.'
                )
            )

        if to_port is None:
            raise serializers.ValidationError(
                {'to_port': _('Empty value is not allowed.')}
            )

        if from_port is None:
            raise serializers.ValidationError(
                {'from_port': _('Empty value is not allowed.')}
            )

        if protocol == 'icmp':
            if from_port is not None and not -1 <= from_port <= 255:
                raise serializers.ValidationError(
                    {
                        'from_port': _('Value should be in range [-1, 255], found %d')
                        % from_port
                    }
                )
            if to_port is not None and not -1 <= to_port <= 255:
                raise serializers.ValidationError(
                    {
                        'to_port': _('Value should be in range [-1, 255], found %d')
                        % to_port
                    }
                )

        elif protocol in ('tcp', 'udp'):
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
                        'from_port': _('Value should be in range [1, 65535], found %d')
                        % from_port
                    }
                )
            if to_port is not None and to_port != -1 and to_port < 1:
                raise serializers.ValidationError(
                    {
                        'to_port': _('Value should be in range [1, 65535], found %d')
                        % to_port
                    }
                )

        elif protocol == '':
            # See also: https://github.com/openstack/neutron/blob/af130e79cbe5d12b7c9f9f4dcbcdc8d972bfcfd4/neutron/db/securitygroups_db.py#L500

            if from_port != -1:
                raise serializers.ValidationError(
                    {
                        'from_port': _(
                            'Port range is not supported if protocol is not specified.'
                        )
                    }
                )

            if to_port != -1:
                raise serializers.ValidationError(
                    {
                        'to_port': _(
                            'Port range is not supported if protocol is not specified.'
                        )
                    }
                )

        else:
            raise serializers.ValidationError(
                {
                    'protocol': _('Value should be one of (tcp, udp, icmp), found %s')
                    % protocol
                }
            )

        return rule


class SecurityGroupRuleCreateSerializer(SecurityGroupRuleSerializer):
    """ Create rules on security group creation """

    def to_internal_value(self, data):
        if 'id' in data:
            raise serializers.ValidationError(
                _('Cannot add existed rule with id %s to new security group')
                % data['id']
            )
        internal_data = super(SecurityGroupRuleSerializer, self).to_internal_value(data)
        return models.SecurityGroupRule(**internal_data)


class SecurityGroupRuleUpdateSerializer(SecurityGroupRuleSerializer):
    def to_internal_value(self, data):
        """ Create new rule if id is not specified, update exist rule if id is specified """
        security_group = self.context['view'].get_object()
        internal_data = super(SecurityGroupRuleSerializer, self).to_internal_value(data)
        if 'id' not in data:
            return models.SecurityGroupRule(
                security_group=security_group, **internal_data
            )
        rule_id = data.pop('id')
        try:
            rule = security_group.rules.get(id=rule_id)
        except models.SecurityGroupRule.DoesNotExist:
            raise serializers.ValidationError(
                {'id': _('Security group does not have rule with id %s.') % rule_id}
            )
        for key, value in internal_data.items():
            setattr(rule, key, value)
        return rule


def validate_duplicate_security_group_rules(rules):
    values = rules.values_list(
        'ethertype',
        'direction',
        'protocol',
        'from_port',
        'to_port',
        'cidr',
        'remote_group',
    )
    if len(set(values)) != len(values):
        raise serializers.ValidationError(
            _('Duplicate security group rules are not allowed.')
        )


class SecurityGroupRuleListUpdateSerializer(serializers.ListSerializer):
    child = SecurityGroupRuleUpdateSerializer()

    @transaction.atomic()
    def save(self, **kwargs):
        security_group = self.context['view'].get_object()
        old_rules_count = security_group.rules.count()
        rules = self.validated_data
        security_group.rules.exclude(id__in=[r.id for r in rules if r.id]).delete()
        for rule in rules:
            rule.save()
        validate_duplicate_security_group_rules(security_group.rules)
        security_group.change_backend_quotas_usage_on_rules_update(old_rules_count)
        return rules


class SecurityGroupSerializer(structure_serializers.BaseResourceActionSerializer):
    rules = SecurityGroupRuleCreateSerializer(many=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.SecurityGroup
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'tenant',
            'tenant_name',
            'tenant_uuid',
            'rules',
        )
        related_paths = ('tenant',)
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + ('service_settings', 'project')
        )
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + ('rules',)
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'openstack-sgp-detail'},
            'tenant': {
                'lookup_field': 'uuid',
                'view_name': 'openstack-tenant-detail',
                'read_only': True,
            },
        }

    def validate_rules(self, value):
        for rule in value:
            if rule.id is not None:
                raise serializers.ValidationError(
                    _('Cannot add existing rule with id %s to new security group')
                    % rule.id
                )
            rule.full_clean(exclude=['security_group'])
        return value

    def validate_name(self, value):
        if value == 'default':
            raise serializers.ValidationError(
                _('Default security group is managed by OpenStack itself.')
            )
        return value

    def validate(self, attrs):
        tenant = self.context['view'].get_object()
        name = attrs['name']

        if tenant.security_groups.filter(name=name):
            raise serializers.ValidationError(
                _('Security group name should be unique.')
            )

        attrs['tenant'] = tenant
        attrs['service_settings'] = tenant.service_settings
        attrs['project'] = tenant.project
        return super(SecurityGroupSerializer, self).validate(attrs)

    def create(self, validated_data):
        rules = validated_data.pop('rules', [])
        with transaction.atomic():
            # quota usage has to be increased only after rules creation,
            # so we cannot execute BaseResourceSerializer create method.
            security_group = super(
                structure_serializers.BaseResourceSerializer, self
            ).create(validated_data)
            for rule in rules:
                security_group.rules.add(rule, bulk=False)
            validate_duplicate_security_group_rules(security_group.rules)
            security_group.increase_backend_quotas_usage()
        return security_group


class SecurityGroupUpdateSerializer(serializers.ModelSerializer):
    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.SecurityGroup
        fields = ('name', 'description')

    def validate_name(self, name):
        if name:
            if name == 'default':
                raise serializers.ValidationError(
                    _('Default security group is managed by OpenStack itself.')
                )

            if self.instance.tenant.security_groups.filter(name=name).exclude(
                pk=self.instance.pk
            ):
                raise serializers.ValidationError(
                    _('Security group name should be unique.')
                )
        return name


ALLOWED_PRIVATE_NETWORKS = (
    IPv4Network('10.0.0.0/8'),
    IPv4Network('172.16.0.0/12'),
    IPv4Network('192.168.0.0/16'),
)


def validate_private_cidr(value, enforced_prefixlen=None):
    try:
        network = IPv4Network(value, strict=True)
    except (AddressValueError, NetmaskValueError, ValueError):
        raise ValidationError(
            message=_('Enter a valid IPv4 address.'), code='invalid',
        )

    if enforced_prefixlen and network.prefixlen != enforced_prefixlen:
        raise ValidationError(
            message=_('Network mask length should be equal to %s.')
            % enforced_prefixlen,
            code='invalid',
        )

    if not any(network.subnet_of(net) for net in ALLOWED_PRIVATE_NETWORKS):
        raise ValidationError(
            message=_('A private network CIDR is expected.'), code='invalid',
        )

    return network.with_prefixlen


def validate_private_subnet_cidr(value):
    return validate_private_cidr(value, 24)


class TenantSerializer(structure_serializers.BaseResourceSerializer):
    quotas = quotas_serializers.QuotaSerializer(many=True, read_only=True)
    subnet_cidr = serializers.CharField(
        default='192.168.42.0/24', initial='192.168.42.0/24', write_only=True,
    )

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Tenant
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'availability_zone',
            'internal_network_id',
            'external_network_id',
            'user_username',
            'user_password',
            'quotas',
            'subnet_cidr',
            'default_volume_type_name',
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + ('internal_network_id', 'external_network_id',)
        )
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + ('user_username', 'subnet_cidr', 'user_password',)
        )
        extra_kwargs = dict(
            name={'max_length': 64},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate_subnet_cidr(self, value):
        return validate_private_subnet_cidr(value)

    def get_fields(self):
        fields = super(TenantSerializer, self).get_fields()
        if not settings.WALDUR_OPENSTACK['TENANT_CREDENTIALS_VISIBLE']:
            for field in ('user_username', 'user_password', 'access_url'):
                if field in fields:
                    del fields[field]

        return fields

    def _validate_service_settings(self, service_settings, project):
        """ Administrator can create tenant only using not shared service settings """
        user = self.context['request'].user
        message = _(
            'You do not have permissions to create tenant in this project using selected service.'
        )
        if service_settings.shared and not user.is_staff:
            raise serializers.ValidationError(message)
        if not service_settings.shared and not structure_permissions._has_admin_access(
            user, project
        ):
            raise serializers.ValidationError(message)

    def validate_security_groups_configuration(self):
        nc_settings = getattr(settings, 'WALDUR_OPENSTACK', {})
        config_groups = nc_settings.get('DEFAULT_SECURITY_GROUPS', [])
        for group in config_groups:
            sg_name = group.get('name')
            if sg_name in (None, ''):
                raise serializers.ValidationError(
                    _(
                        'Skipping misconfigured security group: parameter "name" not found or is empty.'
                    )
                )

            rules = group.get('rules')
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
        if domain in (None, '', 'default'):
            tenants = tenants.filter(
                Q(service_settings__domain='')
                | Q(service_settings__domain__isnull=True)
                | Q(service_settings__domain__iexact='default')
            )
        else:
            tenants = tenants.filter(service_settings__domain=domain)
        return tenants

    def _validate_tenant_name(self, service_settings, tenant_name):
        neighbour_tenants = self._get_neighbour_tenants(service_settings)
        existing_tenant_names = [
            service_settings.options.get('tenant_name', 'admin')
        ] + list(neighbour_tenants.values_list('name', flat=True))
        if tenant_name in existing_tenant_names:
            raise serializers.ValidationError(
                {
                    'name': _(
                        'Name "%s" is already registered. Please choose another one.'
                        % tenant_name
                    ),
                }
            )

    def _validate_username(self, service_settings, username):
        neighbour_tenants = self._get_neighbour_tenants(service_settings)
        existing_usernames = [service_settings.username] + list(
            neighbour_tenants.values_list('user_username', flat=True)
        )
        if username in existing_usernames:
            raise serializers.ValidationError(
                {
                    'user_username': _(
                        'Name "%s" is already registered. Please choose another one.'
                    )
                    % username
                }
            )

        blacklisted_usernames = service_settings.options.get(
            'blacklisted_usernames',
            settings.WALDUR_OPENSTACK['DEFAULT_BLACKLISTED_USERNAMES'],
        )
        if username in blacklisted_usernames:
            raise serializers.ValidationError(
                {
                    'user_username': _(
                        'Name "%s" cannot be used as tenant user username.'
                    )
                    % username
                }
            )

    def validate(self, attrs):
        attrs = super(TenantSerializer, self).validate(attrs)

        if not self.instance:
            self._validate_service_settings(attrs['service_settings'], attrs['project'])

        self.validate_security_groups_configuration()

        if self.instance is not None:
            service_settings = self.instance.service_settings
        else:
            service_settings = attrs['service_settings']

        # validate tenant name
        if self.instance is not None and attrs.get('name'):
            if self.instance.name != attrs['name']:
                self._validate_tenant_name(service_settings, attrs['name'])
        elif attrs.get('name'):
            self._validate_tenant_name(service_settings, attrs['name'])

        # username generation/validation
        if (
            self.instance is not None
            or not settings.WALDUR_OPENSTACK['TENANT_CREDENTIALS_VISIBLE']
        ):
            return attrs
        else:
            if not attrs.get('user_username'):
                attrs['user_username'] = models.Tenant.generate_username(attrs['name'])

            self._validate_username(service_settings, attrs.get('user_username'))

        return attrs

    def create(self, validated_data):
        service_settings = validated_data['service_settings']
        # get availability zone from service settings if it is not defined
        if not validated_data.get('availability_zone'):
            validated_data['availability_zone'] = (
                service_settings.get_option('availability_zone') or ''
            )
        # init tenant user username(if not defined) and password
        slugified_name = slugify(validated_data['name'])[:25]
        if not validated_data.get('user_username'):
            validated_data['user_username'] = models.Tenant.generate_username(
                validated_data['name']
            )
        validated_data['user_password'] = core_utils.pwgen()

        subnet_cidr = validated_data.pop('subnet_cidr')
        with transaction.atomic():
            tenant = super(TenantSerializer, self).create(validated_data)
            network = models.Network.objects.create(
                name=slugified_name + '-int-net',
                description=_('Internal network for tenant %s') % tenant.name,
                tenant=tenant,
                service_settings=tenant.service_settings,
                project=tenant.project,
            )
            models.SubNet.objects.create(
                name=slugified_name + '-sub-net',
                description=_('SubNet for tenant %s internal network') % tenant.name,
                network=network,
                service_settings=tenant.service_settings,
                project=tenant.project,
                cidr=subnet_cidr,
                allocation_pools=_generate_subnet_allocation_pool(subnet_cidr),
                dns_nameservers=service_settings.options.get('dns_nameservers', []),
            )

            nc_settings = getattr(settings, 'WALDUR_OPENSTACK', {})
            config_groups = copy.deepcopy(
                nc_settings.get('DEFAULT_SECURITY_GROUPS', [])
            )

            for group in config_groups:
                sg_name = group.get('name')
                sg_description = group.get('description', None)
                sg = models.SecurityGroup.objects.get_or_create(
                    service_settings=tenant.service_settings,
                    project=tenant.project,
                    tenant=tenant,
                    description=sg_description,
                    name=sg_name,
                )[0]

                for rule in group.get('rules'):
                    if 'icmp_type' in rule:
                        rule['from_port'] = rule.pop('icmp_type')
                    if 'icmp_code' in rule:
                        rule['to_port'] = rule.pop('icmp_code')

                    try:
                        rule = models.SecurityGroupRule(security_group=sg, **rule)
                        rule.full_clean()
                    except serializers.ValidationError as e:
                        logger.error(
                            'Failed to create rule for security group %s: %s.'
                            % (sg_name, e)
                        )
                    else:
                        rule.save()

        return tenant


class _NestedSubNetSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.SubNet
        fields = (
            'name',
            'description',
            'cidr',
            'gateway_ip',
            'allocation_pools',
            'ip_version',
            'enable_dhcp',
        )


class StaticRouteSerializer(serializers.Serializer):
    destination = serializers.CharField()
    nexthop = serializers.IPAddressField()


class RouterSetRoutesSerializer(serializers.Serializer):
    routes = StaticRouteSerializer(many=True)

    def validate(self, attrs):
        fixed_ips = self.instance.fixed_ips
        for route in attrs['routes']:
            nexthop = route['nexthop']
            if nexthop in fixed_ips:
                raise serializers.ValidationError(
                    _('Nexthop %s is used by router.') % nexthop
                )
        return attrs


class RouterSerializer(structure_serializers.BaseResourceSerializer):
    routes = StaticRouteSerializer(many=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    tenant_uuid = serializers.CharField(source='tenant.uuid', read_only=True)
    fixed_ips = serializers.JSONField(read_only=True)

    class Meta:
        model = models.Router
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'tenant',
            'tenant_name',
            'tenant_uuid',
            'routes',
            'fixed_ips',
        )
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'openstack-router-detail'},
            tenant={'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail'},
        )


class PortSerializer(structure_serializers.BaseResourceActionSerializer):
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    tenant_uuid = serializers.CharField(source='tenant.uuid', read_only=True)
    network_name = serializers.CharField(source='network.name', read_only=True)
    network_uuid = serializers.CharField(source='network.uuid', read_only=True)
    allowed_address_pairs = serializers.JSONField(read_only=True)
    floating_ips = serializers.HyperlinkedRelatedField(
        view_name='openstack-fip-detail',
        lookup_field='uuid',
        read_only=True,
        many=True,
    )
    fixed_ips = serializers.JSONField(required=False)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Port
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'fixed_ips',
            'mac_address',
            'allowed_address_pairs',
            'tenant',
            'tenant_name',
            'tenant_uuid',
            'network',
            'network_name',
            'network_uuid',
            'floating_ips',
            'device_id',
            'device_owner',
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                'tenant',
                'allowed_address_pairs',
                'service_settings',
                'project',
                'device_id',
                'device_owner',
            )
        )
        extra_kwargs = dict(
            url={'lookup_field': 'uuid', 'view_name': 'openstack-port-detail'},
            tenant={'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail'},
            network={'lookup_field': 'uuid', 'view_name': 'openstack-network-detail'},
        )

    def validate(self, attrs):
        if self.instance:
            return attrs
        fixed_ips = attrs.get('fixed_ips')
        network: models.Network = self.context['view'].get_object()
        if fixed_ips:
            for fixed_ip in fixed_ips:
                if 'ip_address' not in fixed_ip and 'subnet_id' not in fixed_ip:
                    raise serializers.ValidationError(
                        _('Either ip_address or subnet_id field must be specified')
                    )

                wrong_fields = set(fixed_ip.keys()) - {'ip_address', 'subnet_id'}
                if wrong_fields != set():
                    raise serializers.ValidationError(
                        _(
                            'Only ip_address and subnet_id fields can be specified. Got: %(fields)s'
                        )
                        % {'fields': wrong_fields}
                    )

                if fixed_ip.get('ip_address') == '':
                    raise serializers.ValidationError(
                        _('ip_address field must not be blank. Got %(fixed_ip)s.')
                        % {'fixed_ip': fixed_ip}
                    )

                if fixed_ip.get('subnet_id') == '':
                    raise serializers.ValidationError(
                        _('subnet_id field must not be blank. Got %(fixed_ip)s.')
                        % {'fixed_ip': fixed_ip}
                    )

                if 'ip_address' in fixed_ip:
                    validate_ipv46_address(fixed_ip['ip_address'])

                subnet_backend_id = fixed_ip.get('subnet_id')
                if subnet_backend_id:
                    if not models.SubNet.objects.filter(
                        backend_id=subnet_backend_id, network=network
                    ).exists():
                        raise serializers.ValidationError(
                            {
                                'subnet': _(
                                    'There is no subnet with backend_id [%(backend_id)s] in the network [%(network)s]'
                                )
                                % {'backend_id': subnet_backend_id, 'network': network,}
                            }
                        )
        attrs['service_settings'] = network.service_settings
        attrs['project'] = network.project
        attrs['network'] = network
        attrs['tenant'] = network.tenant

        return super(PortSerializer, self).validate(attrs)


class NetworkSerializer(structure_serializers.BaseResourceActionSerializer):
    subnets = _NestedSubNetSerializer(many=True, read_only=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    tenant_uuid = serializers.CharField(source='tenant.uuid', read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Network
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'tenant',
            'tenant_name',
            'tenant_uuid',
            'is_external',
            'type',
            'segmentation_id',
            'subnets',
            'mtu',
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                'tenant',
                'is_external',
                'type',
                'segmentation_id',
                'mtu',
                'service_settings',
                'project',
            )
        )
        extra_kwargs = dict(
            tenant={'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail'},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs['tenant'] = tenant = self.context['view'].get_object()
        attrs['service_settings'] = tenant.service_settings
        attrs['project'] = tenant.project
        return super(NetworkSerializer, self).validate(attrs)


class SetMtuSerializer(serializers.Serializer):
    mtu = serializers.IntegerField()

    def update(self, network, validated_data):
        network.mtu = validated_data['mtu']
        network.save(update_fields=['mtu'])
        return network


class SubNetSerializer(structure_serializers.BaseResourceActionSerializer):
    cidr = serializers.CharField(
        required=False, initial='192.168.42.0/24', label='CIDR',
    )
    allocation_pools = serializers.JSONField(read_only=True)
    network_name = serializers.CharField(source='network.name', read_only=True)
    tenant = serializers.HyperlinkedRelatedField(
        source='network.tenant',
        view_name='openstack-tenant-detail',
        read_only=True,
        lookup_field='uuid',
    )
    tenant_name = serializers.CharField(source='network.tenant.name', read_only=True)
    dns_nameservers = serializers.JSONField(required=False)
    host_routes = StaticRouteSerializer(many=True, required=False)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.SubNet
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'tenant',
            'tenant_name',
            'network',
            'network_name',
            'cidr',
            'gateway_ip',
            'disable_gateway',
            'allocation_pools',
            'ip_version',
            'enable_dhcp',
            'dns_nameservers',
            'host_routes',
            'is_connected',
        )
        protected_fields = (
            structure_serializers.BaseResourceSerializer.Meta.protected_fields
            + ('cidr',)
        )
        read_only_fields = (
            structure_serializers.BaseResourceSerializer.Meta.read_only_fields
            + (
                'tenant',
                'network',
                'ip_version',
                'enable_dhcp',
                'service_settings',
                'project',
                'is_connected',
            )
        )
        extra_kwargs = dict(
            network={'lookup_field': 'uuid', 'view_name': 'openstack-network-detail'},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate_cidr(self, value):
        if value:
            return validate_private_subnet_cidr(value)

    def validate(self, attrs):
        if attrs.get('disable_gateway') and attrs.get('gateway_ip'):
            raise serializers.ValidationError(
                _(
                    'These parameters are mutually exclusive: disable_gateway and gateway_ip.'
                )
            )

        if self.instance is None:
            attrs['network'] = network = self.context['view'].get_object()
            if network.subnets.count() >= 1:
                raise serializers.ValidationError(
                    _('Internal network cannot have more than one subnet.')
                )
            if 'cidr' not in attrs:
                attrs['cidr'] = '192.168.42.0/24'
            cidr = attrs['cidr']
            if models.SubNet.objects.filter(
                cidr=cidr, network__tenant=network.tenant
            ).exists():
                raise serializers.ValidationError(
                    _('Subnet with cidr "%s" is already registered') % cidr
                )

            attrs['service_settings'] = network.service_settings
            attrs['project'] = network.project
            options = network.service_settings.options
            attrs['allocation_pools'] = _generate_subnet_allocation_pool(cidr)
            attrs.setdefault('dns_nameservers', options.get('dns_nameservers', []))
            self.check_cidr_overlap(network.service_settings, cidr)

        return attrs

    def check_cidr_overlap(self, service_settings, new_cidr):
        cidr_list = list(
            models.SubNet.objects.filter(service_settings=service_settings).values_list(
                'cidr', flat=True
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
        host_routes = validated_data.pop('host_routes', [])
        instance = super(SubNetSerializer, self).update(instance, validated_data)
        instance.host_routes = host_routes
        instance.save()
        return instance


def _generate_subnet_allocation_pool(cidr):
    first_octet, second_octet, third_octet, _ = cidr.split('.', 3)
    subnet_settings = settings.WALDUR_OPENSTACK['SUBNET']
    format_data = {
        'first_octet': first_octet,
        'second_octet': second_octet,
        'third_octet': third_octet,
    }
    return [
        {
            'start': subnet_settings['ALLOCATION_POOL_START'].format(**format_data),
            'end': subnet_settings['ALLOCATION_POOL_END'].format(**format_data),
        }
    ]


class TenantChangePasswordSerializer(serializers.Serializer):
    user_password = serializers.CharField(
        max_length=50,
        allow_blank=True,
        validators=[password_validation.validate_password],
        help_text=_('New tenant user password.'),
    )

    def validate_user_password(self, user_password):
        if self.instance.user_password == user_password:
            raise serializers.ValidationError(
                _('New password cannot match the old password.')
            )

        return user_password

    def update(self, tenant, validated_data):
        tenant.user_password = validated_data['user_password']
        tenant.save(update_fields=['user_password'])
        return tenant
