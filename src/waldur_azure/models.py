from __future__ import unicode_literals

from django.core.validators import MaxValueValidator
from django.db import models
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import models as core_models
from waldur_core.core.fields import JSONField
from waldur_core.quotas.fields import CounterQuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models


class AzureService(structure_models.Service):
    Locations = (('Central US', 'Central US'),
                 ('East US 2', 'East US 2'),
                 ('South Central US', 'South Central US'),
                 ('North Europe', 'North Europe'),
                 ('East Asia', 'East Asia'),
                 ('Southeast Asia', 'Southeast Asia'),
                 ('Japan West', 'Japan West'))

    projects = models.ManyToManyField(
        structure_models.Project, related_name='azure_services', through='AzureServiceProjectLink')

    @classmethod
    def get_url_name(cls):
        return 'azure'

    class Quotas(QuotaModelMixin.Quotas):
        vm_count = CounterQuotaField(
            target_models=lambda: [VirtualMachine],
            path_to_scope='service_project_link.service'
        )


class AzureServiceProjectLink(structure_models.ServiceProjectLink):
    service = models.ForeignKey(AzureService)

    cloud_service_name = models.CharField(max_length=255, blank=True)

    def get_backend(self):
        return super(AzureServiceProjectLink, self).get_backend(
            cloud_service_name=self.cloud_service_name)

    @classmethod
    def get_url_name(cls):
        return 'azure-spl'


class Image(structure_models.GeneralServiceProperty):
    @classmethod
    def get_url_name(cls):
        return 'azure-image'


class Size(object):
    _meta = 'size'

    @classmethod
    def get_url_name(cls):
        return 'azure-size'


class InstanceEndpoint(core_models.BackendModelMixin, models.Model):

    class Protocol(object):
        TCP = 'tcp'
        UDP = 'udp'

        CHOICES = (
            (TCP, 'tcp'),
            (UDP, 'udp'),
        )

    class Name(object):
        SSH = 'SSH'
        RDP = 'Remote Desktop'

        CHOICES = (
            (SSH, 'SSH'),
            (RDP, 'Remote Desktop'),
        )

    local_port = models.IntegerField(validators=[MaxValueValidator(65535)])
    public_port = models.IntegerField(validators=[MaxValueValidator(65535)])
    protocol = models.CharField(max_length=3, blank=True, choices=Protocol.CHOICES)
    name = models.CharField(max_length=255, blank=True, choices=Name.CHOICES)
    instance = models.ForeignKey('VirtualMachine', related_name='endpoints', on_delete=models.PROTECT)

    @classmethod
    def get_backend_fields(cls):
        return super(InstanceEndpoint, cls).get_backend_fields() + (
            'local_port', 'public_port', 'protocol', 'name', 'vm',
        )


class VirtualMachine(structure_models.VirtualMachine):
    service_project_link = models.ForeignKey(
        AzureServiceProjectLink, related_name='virtualmachines', on_delete=models.PROTECT)
    public_ips = JSONField(default=list, help_text=_('List of public IP addresses'), blank=True)
    private_ips = JSONField(default=list, help_text=_('List of private IP addresses'), blank=True)
    user_username = models.CharField(max_length=50)
    user_password = models.CharField(max_length=50)

    @classmethod
    def get_url_name(cls):
        return 'azure-virtualmachine'

    def get_access_url_name(self):
        return 'azure-virtualmachine-rdp'

    @property
    def external_ips(self):
        return self.public_ips

    @property
    def internal_ips(self):
        return self.private_ips

    @classmethod
    def get_backend_fields(cls):
        return super(VirtualMachine, cls).get_backend_fields() + ('public_ips', 'private_ips', 'endpoints')
