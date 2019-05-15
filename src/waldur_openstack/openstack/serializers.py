from __future__ import unicode_literals

import copy
import logging
import re

from django.conf import settings
from django.core import validators
from django.contrib.auth import password_validation
from django.db import transaction
from django.db.models import Q
from django.template.defaultfilters import slugify
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from iptools.ipv4 import validate_cidr

from waldur_core.core import utils as core_utils, serializers as core_serializers
from waldur_core.quotas import serializers as quotas_serializers
from waldur_core.structure import serializers as structure_serializers, permissions as structure_permissions

from . import models
from .backend import OpenStackBackendError


logger = logging.getLogger(__name__)


class ServiceSerializer(core_serializers.ExtraFieldOptionsMixin,
                        core_serializers.RequiredFieldsMixin,
                        structure_serializers.BaseServiceSerializer):

    SERVICE_ACCOUNT_FIELDS = {
        'backend_url': _('Keystone auth URL (e.g. http://keystone.example.com:5000/v3)'),
        'username': _('Administrative user'),
        'domain': _('Domain name. If not defined default domain will be used.'),
        'password': '',
    }
    SERVICE_ACCOUNT_EXTRA_FIELDS = {
        'tenant_name': '',
        'availability_zone': _('Default availability zone for provisioned instances'),
        'volume_availability_zone_name': _('Default availability zone name for provisioned volumes'),
        'external_network_id': _('ID of OpenStack external network that will be connected to tenants'),
        'latitude': _('Latitude of the datacenter (e.g. 40.712784)'),
        'longitude': _('Longitude of the datacenter (e.g. -74.005941)'),
        'access_url': _('Publicly accessible OpenStack dashboard URL'),
        'dns_nameservers': _('Default value for new subnets DNS name servers. Should be defined as list.'),
        'flavor_exclude_regex': _('Flavors matching this regex expression will not be pulled from the backend.'),
        'create_ha_routers': _('Create highly available Neutron routers.'),
    }

    class Meta(structure_serializers.BaseServiceSerializer.Meta):
        model = models.OpenStackService
        required_fields = 'backend_url', 'username', 'password', 'console_type'
        extra_field_options = {
            'backend_url': {
                'label': 'API URL',
                'default_value': 'http://keystone.example.com:5000/v2.0',
            },
            'username': {
                'default_value': 'admin',
            },
            'tenant_name': {
                'label': 'Tenant name',
                'default_value': 'admin',
            },
            'external_network_id': {
                'label': 'Public/gateway network UUID',
            },
            'availability_zone': {
                'placeholder': 'default',
            },
            'volume_availability_zone_name': {
                'label': 'Name of default volume availability zone to use',
            },
            'access_url': {
                'label': 'Access URL',
            },
            'create_ha_routers': {
                'default_value': False,
            },
            'console_type': {
                'default_value': 'novnc',
            },
        }

    def _validate_settings(self, settings):
        backend = settings.get_backend()
        try:
            if not backend.check_admin_tenant():
                raise serializers.ValidationError({
                    'non_field_errors': _('Provided credentials are not for admin tenant.')
                })
        except OpenStackBackendError:
            raise serializers.ValidationError({
                'non_field_errors': _('Unable to validate credentials.')
            })


class ServiceNameSerializer(serializers.Serializer):
    name = serializers.CharField(required=True)


class FlavorSerializer(structure_serializers.BasePropertySerializer):

    class Meta(object):
        model = models.Flavor
        fields = ('url', 'uuid', 'name', 'cores', 'ram', 'disk', 'display_name')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    display_name = serializers.SerializerMethodField()

    def get_display_name(self, flavor):
        return "{} ({} CPU, {} MB RAM, {} MB HDD)".format(
            flavor.name, flavor.cores, flavor.ram, flavor.disk)


class ImageSerializer(structure_serializers.BasePropertySerializer):

    class Meta(object):
        model = models.Image
        fields = ('url', 'uuid', 'name', 'min_disk', 'min_ram')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class ServiceProjectLinkSerializer(structure_serializers.BaseServiceProjectLinkSerializer):

    class Meta(structure_serializers.BaseServiceProjectLinkSerializer.Meta):
        model = models.OpenStackServiceProjectLink
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': 'openstack-detail'},
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
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstack-detail',
        read_only=True,
        lookup_field='uuid')
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstack-spl-detail',
        read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.FloatingIP
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'runtime_state', 'address', 'backend_network_id',
            'tenant', 'tenant_name', 'tenant_uuid')
        related_paths = ('tenant',)
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'runtime_state', 'address', 'description', 'name', 'tenant', 'backend_network_id',
            'service_settings', 'project')
        extra_kwargs = dict(
            tenant={'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail'},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs['tenant'] = tenant = self.context['view'].get_object()
        attrs['service_project_link'] = tenant.service_project_link
        return super(FloatingIPSerializer, self).validate(attrs)


class SecurityGroupRuleSerializer(serializers.ModelSerializer):

    class Meta:
        model = models.SecurityGroupRule
        fields = ('id', 'protocol', 'from_port', 'to_port', 'cidr')

    def validate(self, rule):
        """
        Please note that validate function accepts rule object instead of validated data
        because it is used as a child of list serializer.
        """
        protocol = rule.protocol
        from_port = rule.from_port
        to_port = rule.to_port
        cidr = rule.cidr

        if cidr and not validate_cidr(cidr):
            raise serializers.ValidationError({
                'cidr': _('Expected cidr format: <0-255>.<0-255>.<0-255>.<0-255>/<0-32>')
            })

        if to_port is None:
            raise serializers.ValidationError({
                'to_port': _('Empty value is not allowed.')
            })

        if from_port is None:
            raise serializers.ValidationError({
                'from_port': _('Empty value is not allowed.')
            })

        if protocol == 'icmp':
            if from_port is not None and not -1 <= from_port <= 255:
                raise serializers.ValidationError({
                    'from_port': _('Value should be in range [-1, 255], found %d') % from_port})
            if to_port is not None and not -1 <= to_port <= 255:
                raise serializers.ValidationError({
                    'to_port': _('Value should be in range [-1, 255], found %d') % to_port
                })

        elif protocol in ('tcp', 'udp'):
            if from_port is not None and to_port is not None:
                if from_port > to_port:
                    raise serializers.ValidationError(_('"from_port" should be less or equal to "to_port"'))
            if from_port is not None and from_port < 1:
                raise serializers.ValidationError({
                    'from_port': _('Value should be in range [1, 65535], found %d') % from_port
                })
            if to_port is not None and to_port < 1:
                raise serializers.ValidationError({
                    'to_port': _('Value should be in range [1, 65535], found %d') % to_port
                })

        elif protocol == '':
            # See also: https://github.com/openstack/neutron/blob/af130e79cbe5d12b7c9f9f4dcbcdc8d972bfcfd4/neutron/db/securitygroups_db.py#L500

            if from_port != -1:
                raise serializers.ValidationError({
                    'from_port': _('Port range is not supported if protocol is not specified.')
                })

            if to_port != -1:
                raise serializers.ValidationError({
                    'to_port': _('Port range is not supported if protocol is not specified.')
                })

        else:
            raise serializers.ValidationError({
                'protocol': _('Value should be one of (tcp, udp, icmp), found %s') % protocol
            })

        return rule


class SecurityGroupRuleCreateSerializer(SecurityGroupRuleSerializer):
    """ Create rules on security group creation """

    def to_internal_value(self, data):
        if 'id' in data:
            raise serializers.ValidationError(
                _('Cannot add existed rule with id %s to new security group') % data['id'])
        internal_data = super(SecurityGroupRuleSerializer, self).to_internal_value(data)
        return models.SecurityGroupRule(**internal_data)


class SecurityGroupRuleUpdateSerializer(SecurityGroupRuleSerializer):

    def to_internal_value(self, data):
        """ Create new rule if id is not specified, update exist rule if id is specified """
        security_group = self.context['view'].get_object()
        internal_data = super(SecurityGroupRuleSerializer, self).to_internal_value(data)
        if 'id' not in data:
            return models.SecurityGroupRule(security_group=security_group, **internal_data)
        rule_id = data.pop('id')
        try:
            rule = security_group.rules.get(id=rule_id)
        except models.SecurityGroupRule.DoesNotExist:
            raise serializers.ValidationError({'id': _('Security group does not have rule with id %s.') % rule_id})
        for key, value in internal_data.items():
            setattr(rule, key, value)
        return rule


def validate_duplicate_security_group_rules(rules):
    values = rules.values_list('protocol', 'from_port', 'to_port', 'cidr')
    if len(set(values)) != len(values):
        raise serializers.ValidationError(_('Duplicate security group rules are not allowed.'))


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
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstack-detail',
        read_only=True,
        lookup_field='uuid')
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstack-spl-detail',
        read_only=True)
    rules = SecurityGroupRuleCreateSerializer(many=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.SecurityGroup
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'tenant', 'tenant_name', 'tenant_uuid', 'rules',
        )
        related_paths = ('tenant',)
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'service_settings', 'project')
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + ('rules',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'openstack-sgp-detail'},
            'tenant': {'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail', 'read_only': True},
        }

    def validate_rules(self, value):
        for rule in value:
            if rule.id is not None:
                raise serializers.ValidationError(
                    _('Cannot add existed rule with id %s to new security group') % rule.id)
            rule.full_clean(exclude=['security_group'])
        return value

    def validate_name(self, value):
        if value == 'default':
            raise serializers.ValidationError(_('Default security group is managed by OpenStack itself.'))
        return value

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs['tenant'] = tenant = self.context['view'].get_object()
        attrs['service_project_link'] = tenant.service_project_link
        return super(SecurityGroupSerializer, self).validate(attrs)

    def create(self, validated_data):
        rules = validated_data.pop('rules', [])
        with transaction.atomic():
            # quota usage has to be increased only after rules creation,
            # so we cannot execute BaseResourceSerializer create method.
            security_group = super(structure_serializers.BaseResourceSerializer, self).create(validated_data)
            for rule in rules:
                security_group.rules.add(rule, bulk=False)
            validate_duplicate_security_group_rules(security_group.rules)
            security_group.increase_backend_quotas_usage()
        return security_group


class TenantImportableSerializer(serializers.Serializer):
    backend_id = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    type = serializers.CharField(read_only=True)
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstack-spl-detail',
        queryset=models.OpenStackServiceProjectLink.objects.all(),
        write_only=True)


class TenantImportSerializer(serializers.HyperlinkedModelSerializer):
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstack-spl-detail',
        write_only=True,
        queryset=models.OpenStackServiceProjectLink.objects.all()
    )
    quotas = quotas_serializers.QuotaSerializer(many=True, read_only=True)

    class Meta(object):
        model = models.Tenant
        read_only_fields = ('name', 'uuid', 'availability_zone', 'internal_network_id', 'external_network_id',
                            'user_username', 'user_password', 'quotas')
        fields = read_only_fields + ('service_project_link', 'backend_id')

    @transaction.atomic
    def create(self, validated_data):
        service_project_link = validated_data['service_project_link']
        backend = service_project_link.service.get_backend()
        backend_id = validated_data['backend_id']

        if models.Tenant.objects.filter(
                service_project_link__service__settings=service_project_link.service.settings,
                backend_id=backend_id).exists():
            raise serializers.ValidationError(_('Tenant with ID "%s" is already registered.') % backend_id)

        try:
            tenant = backend.import_tenant(backend_id, service_project_link)
        except OpenStackBackendError as e:
            raise serializers.ValidationError({
                'backend_id': _('Can\'t import tenant with ID %(backend_id)s. Reason: %(reason)s') % {
                    'backend_id': backend_id,
                    'reason': e,
                }
            })

        tenant.user_username = models.Tenant.generate_username(tenant.name)
        tenant.user_password = core_utils.pwgen()
        tenant.save()

        return tenant


subnet_cidr_validator = validators.RegexValidator(
    re.compile(settings.WALDUR_OPENSTACK['SUBNET']['CIDR_REGEX']),
    settings.WALDUR_OPENSTACK['SUBNET']['CIDR_REGEX_EXPLANATION'],
)


class TenantSerializer(structure_serializers.PrivateCloudSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstack-detail',
        read_only=True,
        lookup_field='uuid')
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstack-spl-detail',
        queryset=models.OpenStackServiceProjectLink.objects.all(),
        write_only=True)
    quotas = quotas_serializers.QuotaSerializer(many=True, read_only=True)
    subnet_cidr = serializers.CharField(
        validators=[subnet_cidr_validator], default='192.168.42.0/24', initial='192.168.42.0/24', write_only=True)

    class Meta(structure_serializers.PrivateCloudSerializer.Meta):
        model = models.Tenant
        fields = structure_serializers.PrivateCloudSerializer.Meta.fields + (
            'availability_zone', 'internal_network_id', 'external_network_id',
            'user_username', 'user_password', 'quotas', 'subnet_cidr', 'default_volume_type_name',
        )
        read_only_fields = structure_serializers.PrivateCloudSerializer.Meta.read_only_fields + (
            'internal_network_id', 'external_network_id',
        )
        protected_fields = structure_serializers.PrivateCloudSerializer.Meta.protected_fields + (
            'user_username', 'subnet_cidr', 'user_password',
        )

    def get_fields(self):
        fields = super(TenantSerializer, self).get_fields()
        if not settings.WALDUR_OPENSTACK['TENANT_CREDENTIALS_VISIBLE']:
            for field in ('user_username', 'user_password', 'access_url'):
                if field in fields:
                    del fields[field]

        return fields

    def _validate_service_project_link(self, spl):
        """ Administrator can create tenant only using not shared service settings """
        user = self.context['request'].user
        message = _('You do not have permissions to create tenant in this project using selected service.')
        if spl.service.settings.shared and not user.is_staff:
            raise serializers.ValidationError(message)
        if not spl.service.settings.shared and not structure_permissions._has_admin_access(user, spl.project):
            raise serializers.ValidationError(message)
        return spl

    def validate_security_groups_configuration(self):
        nc_settings = getattr(settings, 'WALDUR_OPENSTACK', {})
        config_groups = nc_settings.get('DEFAULT_SECURITY_GROUPS', [])
        for group in config_groups:
            sg_name = group.get('name')
            if sg_name in (None, ''):
                raise serializers.ValidationError(
                    _('Skipping misconfigured security group: parameter "name" not found or is empty.'))

            rules = group.get('rules')
            if type(rules) not in (list, tuple):
                raise serializers.ValidationError(
                    _('Skipping misconfigured security group: parameter "rules" should be list or tuple.'))

    def _get_neighbour_tenants(self, service_settings):
        domain = service_settings.domain
        backend_url = service_settings.backend_url
        tenants = models.Tenant.objects.filter(service_project_link__service__settings__backend_url=backend_url)
        if domain in (None, '', 'default'):
            tenants = tenants.filter(
                Q(service_project_link__service__settings__domain='') |
                Q(service_project_link__service__settings__domain__isnull=True) |
                Q(service_project_link__service__settings__domain__iexact='default')
            )
        else:
            tenants = tenants.filter(
                service_project_link__service__settings__domain=domain
            )
        return tenants

    def _validate_tenant_name(self, service_settings, tenant_name):
        neighbour_tenants = self._get_neighbour_tenants(service_settings)
        existing_tenant_names = [service_settings.options.get('tenant_name', 'admin')] +\
            list(neighbour_tenants.values_list('name', flat=True))
        if tenant_name in existing_tenant_names:
            raise serializers.ValidationError({
                'name': _('Name "%s" is already registered. Please choose another one.' % tenant_name),
            })

    def _validate_username(self, service_settings, username):
        neighbour_tenants = self._get_neighbour_tenants(service_settings)
        existing_usernames = [service_settings.username] + \
            list(neighbour_tenants.values_list('user_username', flat=True))
        if username in existing_usernames:
            raise serializers.ValidationError({
                'user_username': _('Name "%s" is already registered. Please choose another one.') % username
            })

        blacklisted_usernames = service_settings.options.get(
            'blacklisted_usernames', settings.WALDUR_OPENSTACK['DEFAULT_BLACKLISTED_USERNAMES'])
        if username in blacklisted_usernames:
            raise serializers.ValidationError({
                'user_username': _('Name "%s" cannot be used as tenant user username.') % username
            })

    def validate(self, attrs):
        attrs = super(TenantSerializer, self).validate(attrs)

        if not self.instance:
            self._validate_service_project_link(attrs['service_project_link'])

        self.validate_security_groups_configuration()

        if self.instance is not None:
            service_settings = self.instance.service_project_link.service.settings
        else:
            service_settings = attrs['service_project_link'].service.settings

        # validate tenant name
        if self.instance is not None and attrs.get('name'):
            if self.instance.name != attrs['name']:
                self._validate_tenant_name(service_settings, attrs['name'])
        else:
            self._validate_tenant_name(service_settings, attrs['name'])

        # username generation/validation
        if self.instance is not None or not settings.WALDUR_OPENSTACK['TENANT_CREDENTIALS_VISIBLE']:
            return attrs
        else:
            if not attrs.get('user_username'):
                attrs['user_username'] = models.Tenant.generate_username(attrs['name'])

            self._validate_username(service_settings, attrs.get('user_username'))

        return attrs

    def create(self, validated_data):
        spl = validated_data['service_project_link']
        # get availability zone from service settings if it is not defined
        if not validated_data.get('availability_zone'):
            validated_data['availability_zone'] = spl.service.settings.get_option('availability_zone') or ''
        # init tenant user username(if not defined) and password
        slugified_name = slugify(validated_data['name'])[:25]
        if not validated_data.get('user_username'):
            validated_data['user_username'] = models.Tenant.generate_username(validated_data['name'])
        validated_data['user_password'] = core_utils.pwgen()

        subnet_cidr = validated_data.pop('subnet_cidr')
        with transaction.atomic():
            tenant = super(TenantSerializer, self).create(validated_data)
            network = models.Network.objects.create(
                name=slugified_name + '-int-net',
                description=_('Internal network for tenant %s') % tenant.name,
                tenant=tenant,
                service_project_link=tenant.service_project_link,
            )
            models.SubNet.objects.create(
                name=slugified_name + '-sub-net',
                description=_('SubNet for tenant %s internal network') % tenant.name,
                network=network,
                service_project_link=tenant.service_project_link,
                cidr=subnet_cidr,
                allocation_pools=_generate_subnet_allocation_pool(subnet_cidr),
                dns_nameservers=spl.service.settings.options.get('dns_nameservers', [])
            )

            nc_settings = getattr(settings, 'WALDUR_OPENSTACK', {})
            config_groups = copy.deepcopy(nc_settings.get('DEFAULT_SECURITY_GROUPS', []))

            for group in config_groups:
                sg_name = group.get('name')
                sg_description = group.get('description', None)
                sg = models.SecurityGroup.objects.get_or_create(
                    service_project_link=tenant.service_project_link,
                    tenant=tenant,
                    description=sg_description,
                    name=sg_name)[0]

                for rule in group.get('rules'):
                    if 'icmp_type' in rule:
                        rule['from_port'] = rule.pop('icmp_type')
                    if 'icmp_code' in rule:
                        rule['to_port'] = rule.pop('icmp_code')

                    try:
                        rule = models.SecurityGroupRule(security_group=sg, **rule)
                        rule.full_clean()
                    except serializers.ValidationError as e:
                        logger.error('Failed to create rule for security group %s: %s.' % (sg_name, e))
                    else:
                        rule.save()

        return tenant


class _NestedSubNetSerializer(serializers.ModelSerializer):

    class Meta(object):
        model = models.SubNet
        fields = ('name', 'description', 'cidr', 'gateway_ip', 'allocation_pools', 'ip_version', 'enable_dhcp')


class NetworkSerializer(structure_serializers.BaseResourceActionSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstack-detail',
        read_only=True,
        lookup_field='uuid')
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstack-spl-detail',
        read_only=True)
    subnets = _NestedSubNetSerializer(many=True, read_only=True)
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.Network
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'tenant', 'tenant_name', 'is_external', 'type', 'segmentation_id', 'subnets')
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'tenant', 'is_external', 'type', 'segmentation_id', 'service_settings', 'project')
        extra_kwargs = dict(
            tenant={'lookup_field': 'uuid', 'view_name': 'openstack-tenant-detail'},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate(self, attrs):
        # Skip validation on update
        if self.instance:
            return attrs

        attrs['tenant'] = tenant = self.context['view'].get_object()
        attrs['service_project_link'] = tenant.service_project_link
        return super(NetworkSerializer, self).validate(attrs)


class SubNetSerializer(structure_serializers.BaseResourceActionSerializer):
    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name='openstack-detail',
        read_only=True,
        lookup_field='uuid')
    service_project_link = serializers.HyperlinkedRelatedField(
        view_name='openstack-spl-detail',
        read_only=True)
    cidr = serializers.CharField(
        validators=[subnet_cidr_validator], required=False, initial='192.168.42.0/24', label='CIDR')
    allocation_pools = serializers.JSONField(read_only=True)
    network_name = serializers.CharField(source='network.name', read_only=True)
    tenant = serializers.HyperlinkedRelatedField(
        source='network.tenant',
        view_name='openstack-tenant-detail',
        read_only=True,
        lookup_field='uuid')
    tenant_name = serializers.CharField(source='network.tenant.name', read_only=True)
    dns_nameservers = serializers.JSONField(read_only=True)

    class Meta(structure_serializers.BaseResourceSerializer.Meta):
        model = models.SubNet
        fields = structure_serializers.BaseResourceSerializer.Meta.fields + (
            'tenant', 'tenant_name', 'network', 'network_name', 'cidr',
            'gateway_ip', 'allocation_pools', 'ip_version', 'enable_dhcp', 'dns_nameservers')
        protected_fields = structure_serializers.BaseResourceSerializer.Meta.protected_fields + ('cidr',)
        read_only_fields = structure_serializers.BaseResourceSerializer.Meta.read_only_fields + (
            'tenant', 'network', 'gateway_ip', 'ip_version', 'enable_dhcp', 'service_settings', 'project')
        extra_kwargs = dict(
            network={'lookup_field': 'uuid', 'view_name': 'openstack-network-detail'},
            **structure_serializers.BaseResourceSerializer.Meta.extra_kwargs
        )

    def validate(self, attrs):
        if self.instance is None:
            attrs['network'] = network = self.context['view'].get_object()
            if network.subnets.count() >= 1:
                raise serializers.ValidationError(_('Internal network cannot have more than one subnet.'))
            if 'cidr' not in attrs:
                attrs['cidr'] = '192.168.42.0/24'
            cidr = attrs['cidr']
            if models.SubNet.objects.filter(cidr=cidr, network__tenant=network.tenant).exists():
                raise serializers.ValidationError(_('Subnet with cidr "%s" is already registered') % cidr)

            attrs['service_project_link'] = network.service_project_link
            options = network.service_project_link.service.settings.options
            attrs['allocation_pools'] = _generate_subnet_allocation_pool(cidr)
            attrs['dns_nameservers'] = options.get('dns_nameservers', [])

        return attrs


def _generate_subnet_allocation_pool(cidr):
    first_octet, second_octet, third_octet, _ = cidr.split('.', 3)
    subnet_settings = settings.WALDUR_OPENSTACK['SUBNET']
    format_data = {'first_octet': first_octet, 'second_octet': second_octet, 'third_octet': third_octet}
    return [{
        'start': subnet_settings['ALLOCATION_POOL_START'].format(**format_data),
        'end': subnet_settings['ALLOCATION_POOL_END'].format(**format_data),
    }]


class TenantChangePasswordSerializer(serializers.Serializer):
    user_password = serializers.CharField(max_length=50,
                                          allow_blank=True,
                                          validators=[password_validation.validate_password],
                                          help_text=_('New tenant user password.'))

    def validate_user_password(self, user_password):
        if self.instance.user_password == user_password:
            raise serializers.ValidationError(_('New password cannot match the old password.'))

        return user_password

    def update(self, tenant, validated_data):
        tenant.user_password = validated_data['user_password']
        tenant.save(update_fields=['user_password'])
        return tenant
