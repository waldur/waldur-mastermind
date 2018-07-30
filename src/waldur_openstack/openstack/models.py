from __future__ import unicode_literals

from django.db import models
from django.template.defaultfilters import slugify
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from waldur_core.core.fields import JSONField
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from six.moves import urllib
from waldur_core.core import models as core_models, utils as core_utils
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.quotas.fields import QuotaField, UsageAggregatorQuotaField, CounterQuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models

from waldur_openstack.openstack_base import models as openstack_base_models


class ServiceUsageAggregatorQuotaField(UsageAggregatorQuotaField):
    def __init__(self, **kwargs):
        super(ServiceUsageAggregatorQuotaField, self).__init__(
            get_children=lambda service: Tenant.objects.filter(
                service_project_link__service=service
            ), **kwargs)


class OpenStackService(structure_models.Service):
    projects = models.ManyToManyField(
        structure_models.Project, related_name='openstack_services', through='OpenStackServiceProjectLink')

    class Meta:
        unique_together = ('customer', 'settings')
        verbose_name = _('OpenStack provider')
        verbose_name_plural = _('OpenStack providers')

    class Quotas(QuotaModelMixin.Quotas):
        tenant_count = CounterQuotaField(
            target_models=lambda: [Tenant],
            path_to_scope='service_project_link.service'
        )
        vcpu = ServiceUsageAggregatorQuotaField()
        ram = ServiceUsageAggregatorQuotaField()
        storage = ServiceUsageAggregatorQuotaField()
        instances = ServiceUsageAggregatorQuotaField()
        security_group_count = ServiceUsageAggregatorQuotaField()
        security_group_rule_count = ServiceUsageAggregatorQuotaField()
        floating_ip_count = ServiceUsageAggregatorQuotaField()
        volumes = ServiceUsageAggregatorQuotaField()
        snapshots = ServiceUsageAggregatorQuotaField()

    @classmethod
    def get_url_name(cls):
        return 'openstack'


class OpenStackServiceProjectLink(structure_models.ServiceProjectLink):

    service = models.ForeignKey(OpenStackService)

    class Meta(structure_models.ServiceProjectLink.Meta):
        verbose_name = _('OpenStack provider project link')
        verbose_name_plural = _('OpenStack provider project links')

    @classmethod
    def get_url_name(cls):
        return 'openstack-spl'

    # XXX: Hack for statistics: return quotas of tenants as quotas of SPLs.
    @classmethod
    def get_sum_of_quotas_as_dict(cls, spls, quota_names=None, fields=['usage', 'limit']):
        tenants = Tenant.objects.filter(service_project_link__in=spls)
        return Tenant.get_sum_of_quotas_as_dict(tenants, quota_names=quota_names, fields=fields)


class Flavor(LoggableMixin, structure_models.ServiceProperty):
    cores = models.PositiveSmallIntegerField(help_text=_('Number of cores in a VM'))
    ram = models.PositiveIntegerField(help_text=_('Memory size in MiB'))
    disk = models.PositiveIntegerField(help_text=_('Root disk size in MiB'))

    @classmethod
    def get_url_name(cls):
        return 'openstack-flavor'

    @classmethod
    def get_backend_fields(cls):
        readonly_fields = super(Flavor, cls).get_backend_fields()
        return readonly_fields + ('cores', 'ram', 'disk')


class Image(openstack_base_models.BaseImage):
    @classmethod
    def get_url_name(cls):
        return 'openstack-image'


class SecurityGroup(structure_models.SubResource):
    service_project_link = models.ForeignKey(
        OpenStackServiceProjectLink, related_name='security_groups')
    tenant = models.ForeignKey('Tenant', related_name='security_groups')

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'openstack-sgp'

    def increase_backend_quotas_usage(self, validate=True):
        self.tenant.add_quota_usage(self.tenant.Quotas.security_group_count, 1, validate=validate)
        self.tenant.add_quota_usage(self.tenant.Quotas.security_group_rule_count, self.rules.count(), validate=validate)

    def decrease_backend_quotas_usage(self):
        self.tenant.add_quota_usage(self.tenant.Quotas.security_group_count, -1)
        self.tenant.add_quota_usage(self.tenant.Quotas.security_group_rule_count, -self.rules.count())

    def change_backend_quotas_usage_on_rules_update(self, old_rules_count, validate=True):
        count = self.rules.count() - old_rules_count
        self.tenant.add_quota_usage(self.tenant.Quotas.security_group_rule_count, count, validate=validate)

    @classmethod
    def get_backend_fields(cls):
        return super(SecurityGroup, cls).get_backend_fields() + ('name', 'description')


class SecurityGroupRule(openstack_base_models.BaseSecurityGroupRule):
    security_group = models.ForeignKey(SecurityGroup, related_name='rules')


@python_2_unicode_compatible
class FloatingIP(core_models.RuntimeStateMixin, structure_models.SubResource):
    service_project_link = models.ForeignKey(
        OpenStackServiceProjectLink, related_name='floating_ips')
    tenant = models.ForeignKey('Tenant', related_name='floating_ips')
    address = models.GenericIPAddressField(null=True, blank=True, protocol='IPv4', default=None)
    backend_network_id = models.CharField(max_length=255, editable=False)

    tracker = FieldTracker()

    class Meta:
        unique_together = ('tenant', 'address')
        verbose_name = _('Floating IP')
        verbose_name_plural = _('Floating IPs')

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'openstack-fip'

    def __str__(self):
        return '%s:%s (%s)' % (self.address, self.runtime_state, self.service_project_link)

    @classmethod
    def get_backend_fields(cls):
        return super(FloatingIP, cls).get_backend_fields() + ('name', 'description', 'address', 'backend_network_id',
                                                              'runtime_state')

    def increase_backend_quotas_usage(self, validate=True):
        self.tenant.add_quota_usage(self.tenant.Quotas.floating_ip_count, 1, validate=validate)

    def decrease_backend_quotas_usage(self):
        self.tenant.add_quota_usage(self.tenant.Quotas.floating_ip_count, -1)


class Tenant(structure_models.PrivateCloud):

    class Quotas(QuotaModelMixin.Quotas):
        vcpu = QuotaField(default_limit=20, is_backend=True)
        ram = QuotaField(default_limit=51200, is_backend=True)
        storage = QuotaField(default_limit=1024000, is_backend=True)
        instances = QuotaField(default_limit=30, is_backend=True)
        security_group_count = QuotaField(default_limit=100, is_backend=True)
        security_group_rule_count = QuotaField(default_limit=100, is_backend=True)
        floating_ip_count = QuotaField(default_limit=50, is_backend=True)
        volumes = QuotaField(default_limit=50, is_backend=True)
        volumes_size = QuotaField(is_backend=True)
        snapshots = QuotaField(default_limit=50, is_backend=True)
        snapshots_size = QuotaField(is_backend=True)
        network_count = QuotaField(default_limit=10, is_backend=True)
        subnet_count = QuotaField(default_limit=10, is_backend=True)

    # backend_id is nullable on purpose, otherwise
    # it wouldn't be possible to put a unique constraint on it
    backend_id = models.CharField(max_length=255, blank=True, null=True)

    service_project_link = models.ForeignKey(
        OpenStackServiceProjectLink, related_name='tenants', on_delete=models.PROTECT)

    internal_network_id = models.CharField(max_length=64, blank=True)
    external_network_id = models.CharField(max_length=64, blank=True)
    availability_zone = models.CharField(
        max_length=100, blank=True,
        help_text=_('Optional availability group. Will be used for all instances provisioned in this tenant')
    )
    user_username = models.CharField(max_length=50, blank=True)
    user_password = models.CharField(max_length=50, blank=True)

    tracker = FieldTracker()

    class Meta(object):
        unique_together = ('service_project_link', 'backend_id')

    @classmethod
    def generate_username(cls, name):
        """
        Generates random valid tenant user name based on tenant name
        :param name: tenant name
        :return: username
        """
        return slugify(name)[:25] + '-user-%s' % core_utils.pwgen(4)

    def get_backend(self):
        return self.service_project_link.service.get_backend(tenant_id=self.backend_id)

    def get_log_fields(self):
        return super(Tenant, self).get_log_fields() + ('extra_configuration',)

    @classmethod
    def get_backend_fields(cls):
        return super(Tenant, cls).get_backend_fields() + ('name', 'description', 'error_message', 'runtime_state')

    def get_access_url(self):
        settings = self.service_project_link.service.settings
        access_url = settings.get_option('access_url')
        if access_url:
            return access_url

        if settings.backend_url:
            parsed = urllib.parse.urlparse(settings.backend_url)
            return '%s://%s/dashboard' % (parsed.scheme, parsed.hostname)

    def format_quota(self, name, limit):
        if name == self.Quotas.vcpu.name:
            return int(limit)
        elif name in (self.Quotas.storage.name, self.Quotas.ram.name):
            return _('%s GB') % int(limit / 1024)
        else:
            return limit


class Network(core_models.RuntimeStateMixin, structure_models.SubResource):
    service_project_link = models.ForeignKey(
        OpenStackServiceProjectLink, related_name='networks', on_delete=models.PROTECT)
    tenant = models.ForeignKey(Tenant, related_name='networks')
    is_external = models.BooleanField(default=False)
    type = models.CharField(max_length=50, blank=True)
    segmentation_id = models.IntegerField(null=True)

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'openstack-network'

    def increase_backend_quotas_usage(self, validate=True):
        self.tenant.add_quota_usage(self.tenant.Quotas.network_count, 1, validate=validate)

    def decrease_backend_quotas_usage(self):
        self.tenant.add_quota_usage(self.tenant.Quotas.network_count, -1)

    @classmethod
    def get_backend_fields(cls):
        return super(Network, cls).get_backend_fields() + ('name', 'description', 'is_external', 'type',
                                                           'segmentation_id', 'runtime_state')


class SubNet(structure_models.SubResource):
    service_project_link = models.ForeignKey(
        OpenStackServiceProjectLink, related_name='subnets', on_delete=models.PROTECT)
    network = models.ForeignKey(Network, related_name='subnets')
    cidr = models.CharField(max_length=32, blank=True)
    gateway_ip = models.GenericIPAddressField(protocol='IPv4', null=True)
    allocation_pools = JSONField(default=dict)
    ip_version = models.SmallIntegerField(default=4)
    enable_dhcp = models.BooleanField(default=True)
    dns_nameservers = JSONField(default=list, help_text=_('List of DNS name servers associated with the subnet.'))

    class Meta:
        verbose_name = _('Subnet')
        verbose_name_plural = _('Subnets')

    def get_backend(self):
        return self.network.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'openstack-subnet'

    def increase_backend_quotas_usage(self, validate=True):
        self.network.tenant.add_quota_usage(self.network.tenant.Quotas.subnet_count, 1, validate=validate)

    def decrease_backend_quotas_usage(self):
        self.network.tenant.add_quota_usage(self.network.tenant.Quotas.subnet_count, -1)

    @classmethod
    def get_backend_fields(cls):
        return super(SubNet, cls).get_backend_fields() + ('name', 'description', 'allocation_pools', 'cidr',
                                                          'ip_version', 'enable_dhcp', 'gateway_ip', 'dns_nameservers')


class CustomerOpenStack(TimeStampedModel):
    settings = models.ForeignKey(structure_models.ServiceSettings,
                                 on_delete=models.CASCADE,
                                 limit_choices_to={'shared': True, 'type': 'OpenStack'}, )
    customer = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    external_network_id = models.CharField(_('OpenStack external network ID'), max_length=255)

    class Meta(object):
        verbose_name = _('Organization OpenStack settings')
        verbose_name_plural = _('Organization OpenStack settings')
        unique_together = ('settings', 'customer')
