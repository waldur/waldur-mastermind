from django.db import models

from waldur_core.quotas.fields import QuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models


class TestNewInstance(QuotaModelMixin, structure_models.VirtualMachine):
    __test__ = False

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
    pass


class TestVolume(structure_models.Volume):
    pass


class TestSnapshot(structure_models.Snapshot):
    pass
