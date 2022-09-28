from urllib.parse import urlparse

from django.core import validators
from django.db import models
from django.template.defaultfilters import slugify
from django.utils.translation import gettext_lazy as _
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.fields import JSONField
from waldur_core.core.models import StateMixin
from waldur_core.logging.loggers import LoggableMixin
from waldur_core.quotas.fields import QuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models
from waldur_openstack.openstack_base import models as openstack_base_models


class Flavor(StateMixin, LoggableMixin, structure_models.ServiceProperty):
    cores = models.PositiveSmallIntegerField(help_text=_('Number of cores in a VM'))
    ram = models.PositiveIntegerField(help_text=_('Memory size in MiB'))
    disk = models.PositiveIntegerField(help_text=_('Root disk size in MiB'))

    class Permissions:
        customer_path = 'settings__customer'

    @classmethod
    def get_url_name(cls):
        return 'openstack-flavor'

    @classmethod
    def get_backend_fields(cls):
        readonly_fields = super(Flavor, cls).get_backend_fields()
        return readonly_fields + ('cores', 'ram', 'disk')

    def get_backend(self):
        return self.settings.get_backend()


class Image(openstack_base_models.BaseImage):
    @classmethod
    def get_url_name(cls):
        return 'openstack-image'


class VolumeType(openstack_base_models.BaseVolumeType):
    @classmethod
    def get_url_name(cls):
        return 'openstack-volume-type'


class ServerGroup(openstack_base_models.BaseServerGroup, structure_models.SubResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to='Tenant', related_name='server_groups'
    )

    tracker = FieldTracker()

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'openstack-server-group'

    @classmethod
    def get_backend_fields(cls):
        return super(ServerGroup, cls).get_backend_fields() + (
            'name',
            'policy',
        )


class SecurityGroup(structure_models.SubResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to='Tenant', related_name='security_groups'
    )

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'openstack-sgp'

    def increase_backend_quotas_usage(self, validate=True):
        self.tenant.add_quota_usage(
            self.tenant.Quotas.security_group_count, 1, validate=validate
        )
        self.tenant.add_quota_usage(
            self.tenant.Quotas.security_group_rule_count,
            self.rules.count(),
            validate=validate,
        )

    def decrease_backend_quotas_usage(self):
        self.tenant.add_quota_usage(self.tenant.Quotas.security_group_count, -1)
        self.tenant.add_quota_usage(
            self.tenant.Quotas.security_group_rule_count, -self.rules.count()
        )

    def change_backend_quotas_usage_on_rules_update(
        self, old_rules_count, validate=True
    ):
        count = self.rules.count() - old_rules_count
        self.tenant.add_quota_usage(
            self.tenant.Quotas.security_group_rule_count, count, validate=validate
        )

    @classmethod
    def get_backend_fields(cls):
        return super(SecurityGroup, cls).get_backend_fields() + ('name', 'description')


class SecurityGroupRule(
    core_models.LoggableMixin, openstack_base_models.BaseSecurityGroupRule
):
    security_group = models.ForeignKey(
        on_delete=models.CASCADE, to=SecurityGroup, related_name='rules'
    )
    remote_group = models.ForeignKey(
        on_delete=models.CASCADE,
        to=SecurityGroup,
        related_name='+',
        null=True,
        blank=True,
    )
    tracker = FieldTracker()

    def get_log_fields(self):
        return (
            'security_group',
            'protocol',
            'from_port',
            'to_port',
            'cidr',
            'direction',
            'ethertype',
            'backend_id',
        )


class FloatingIP(core_models.RuntimeStateMixin, structure_models.SubResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to='Tenant', related_name='floating_ips'
    )
    address = models.GenericIPAddressField(
        null=True, blank=True, protocol='IPv4', default=None
    )
    backend_network_id = models.CharField(max_length=255, editable=False)
    port = models.ForeignKey(
        on_delete=models.SET_NULL,
        to='Port',
        related_name='floating_ips',
        blank=True,
        null=True,
    )

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
        return '%s:%s (%s)' % (
            self.address,
            self.runtime_state,
            self.service_settings,
        )

    @classmethod
    def get_backend_fields(cls):
        return super(FloatingIP, cls).get_backend_fields() + (
            'name',
            'description',
            'address',
            'backend_network_id',
            'runtime_state',
            'port',
        )

    def increase_backend_quotas_usage(self, validate=True):
        self.tenant.add_quota_usage(
            self.tenant.Quotas.floating_ip_count, 1, validate=validate
        )

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
            'Optional availability group. Will be used for all instances provisioned in this tenant'
        ),
    )
    default_volume_type_name = models.CharField(
        max_length=100,
        blank=True,
        help_text=_('Volume type name to use when creating volumes.'),
    )
    user_username = models.CharField(max_length=50, blank=True)
    user_password = models.CharField(max_length=50, blank=True)

    tracker = FieldTracker()

    class Meta:
        unique_together = ('service_settings', 'backend_id')

    @classmethod
    def generate_username(cls, name):
        """
        Generates random valid tenant user name based on tenant name
        :param name: tenant name
        :return: username
        """
        return slugify(name)[:25] + '-user-%s' % core_utils.pwgen(4)

    def get_backend(self):
        return self.service_settings.get_backend(tenant_id=self.backend_id)

    @classmethod
    def get_backend_fields(cls):
        return super(Tenant, cls).get_backend_fields() + (
            'name',
            'description',
            'error_message',
            'runtime_state',
        )

    def get_access_url(self):
        settings = self.service_settings
        access_url = settings.get_option('access_url')
        if access_url:
            return access_url

        if settings.backend_url:
            parsed = urlparse(settings.backend_url)
            return '%s://%s/dashboard' % (parsed.scheme, parsed.hostname)

    def format_quota(self, name, limit):
        if name == self.Quotas.vcpu.name:
            return int(limit)
        elif name in (self.Quotas.storage.name, self.Quotas.ram.name):
            return _('%s GB') % int(limit / 1024)
        else:
            return limit

    @classmethod
    def get_quotas_names(cls):
        volume_type_names = VolumeType.objects.values_list('name', flat=True).distinct()
        return super(Tenant, cls).get_quotas_names() + [
            f'gigabytes_{name}' for name in volume_type_names
        ]


class Router(structure_models.SubResource):
    tenant: Tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name='routers'
    )
    routes = JSONField(default=list)
    fixed_ips = JSONField(default=list)

    tracker = FieldTracker()

    def get_backend(self):
        return self.tenant.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'openstack-router'


class Network(core_models.RuntimeStateMixin, structure_models.SubResource):
    tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name='networks'
    )
    is_external = models.BooleanField(default=False)
    type = models.CharField(max_length=50, blank=True)
    segmentation_id = models.IntegerField(null=True)
    mtu = models.IntegerField(
        null=True,
        help_text=_(
            'The maximum transmission unit (MTU) value to address fragmentation.'
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
        return 'openstack-network'

    def increase_backend_quotas_usage(self, validate=True):
        self.tenant.add_quota_usage(
            self.tenant.Quotas.network_count, 1, validate=validate
        )

    def decrease_backend_quotas_usage(self):
        self.tenant.add_quota_usage(self.tenant.Quotas.network_count, -1)

    @classmethod
    def get_backend_fields(cls):
        return super(Network, cls).get_backend_fields() + (
            'name',
            'description',
            'is_external',
            'type',
            'segmentation_id',
            'runtime_state',
            'mtu',
        )


class SubNet(openstack_base_models.BaseSubNet, structure_models.SubResource):
    network = models.ForeignKey(
        on_delete=models.CASCADE, to=Network, related_name='subnets'
    )
    disable_gateway = models.BooleanField(default=False)
    host_routes = JSONField(
        default=list,
        help_text=_('List of additional routes for the subnet.'),
    )

    class Meta:
        verbose_name = _('Subnet')
        verbose_name_plural = _('Subnets')

    def get_backend(self):
        return self.network.get_backend()

    @classmethod
    def get_url_name(cls):
        return 'openstack-subnet'

    def increase_backend_quotas_usage(self, validate=True):
        self.network.tenant.add_quota_usage(
            self.network.tenant.Quotas.subnet_count, 1, validate=validate
        )

    def decrease_backend_quotas_usage(self):
        self.network.tenant.add_quota_usage(self.network.tenant.Quotas.subnet_count, -1)

    @classmethod
    def get_backend_fields(cls):
        return super(SubNet, cls).get_backend_fields() + (
            'name',
            'description',
            'allocation_pools',
            'cidr',
            'ip_version',
            'enable_dhcp',
            'gateway_ip',
            'dns_nameservers',
            'host_routes',
            'is_connected',
        )

    def get_log_fields(self):
        return super(SubNet, self).get_log_fields() + ('network',)


class Port(structure_models.SubResource, openstack_base_models.Port):
    tenant: Tenant = models.ForeignKey(
        on_delete=models.CASCADE, to=Tenant, related_name='ports'
    )
    network = models.ForeignKey(
        on_delete=models.CASCADE,
        to=Network,
        related_name='ports',
        null=True,
        blank=True,
    )
    port_security_enabled = models.BooleanField(default=True)
    security_groups = models.ManyToManyField(SecurityGroup, related_name='ports')

    @classmethod
    def get_url_name(cls):
        return 'openstack-port'

    def __str__(self):
        return self.name


class CustomerOpenStack(TimeStampedModel):
    settings = models.ForeignKey(
        structure_models.ServiceSettings,
        on_delete=models.CASCADE,
        limit_choices_to={'shared': True, 'type': 'OpenStack'},
    )
    customer = models.ForeignKey(on_delete=models.CASCADE, to=structure_models.Customer)
    external_network_id = models.CharField(
        _('OpenStack external network ID'), max_length=255
    )

    class Meta:
        verbose_name = _('Organization OpenStack settings')
        verbose_name_plural = _('Organization OpenStack settings')
        unique_together = ('settings', 'customer')
