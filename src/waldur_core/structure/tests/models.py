from django.db import models

from waldur_core.quotas.fields import QuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models


class TestService(structure_models.Service):
    projects = models.ManyToManyField(structure_models.Project, through='TestServiceProjectLink')

    @classmethod
    def get_url_name(cls):
        return 'test'


class TestServiceProjectLink(structure_models.ServiceProjectLink):
    service = models.ForeignKey(TestService)

    class Quotas(QuotaModelMixin.Quotas):
        vcpu = QuotaField(default_limit=20, is_backend=True)
        ram = QuotaField(default_limit=51200, is_backend=True)
        storage = QuotaField(default_limit=1024000, is_backend=True)
        instances = QuotaField(default_limit=30, is_backend=True)
        security_group_count = QuotaField(default_limit=100, is_backend=True)
        security_group_rule_count = QuotaField(default_limit=100, is_backend=True)
        floating_ip_count = QuotaField(default_limit=50, is_backend=True)

    @classmethod
    def get_url_name(cls):
        return 'test-spl'


class TestNewInstance(QuotaModelMixin, structure_models.VirtualMachine):

    service_project_link = models.ForeignKey(TestServiceProjectLink, on_delete=models.PROTECT)
    flavor_name = models.CharField(max_length=255, blank=True)

    class Quotas(QuotaModelMixin.Quotas):
        test_quota = QuotaField(default_limit=1)

    @classmethod
    def get_url_name(cls):
        return 'test-new-instances'

    @property
    def internal_ips(self):
        return ['127.0.0.1']

    @property
    def external_ips(self):
        return ['8.8.8.8']


class TestSubResource(structure_models.SubResource):

    service_project_link = models.ForeignKey(TestServiceProjectLink, on_delete=models.PROTECT)


class TestVolume(structure_models.Volume):

    service_project_link = models.ForeignKey(TestServiceProjectLink, on_delete=models.PROTECT)


class TestSnapshot(structure_models.Snapshot):

    service_project_link = models.ForeignKey(TestServiceProjectLink, on_delete=models.PROTECT)
