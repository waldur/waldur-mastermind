from typing import cast

from django.db import models
from model_utils import FieldTracker
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core import models as core_models
from waldur_core.quotas.fields import QuotaField
from waldur_core.quotas.models import QuotaModelMixin
from waldur_core.structure import models as structure_models


class TestNewInstance(
    QuotaModelMixin, structure_models.VirtualMachine, core_models.AvailableMixin
):
    __test__ = False

    class Meta(structure_models.VirtualMachine.Meta):
        pass

    flavor_name = models.CharField(max_length=255, blank=True)
    tracker = cast(FieldInstanceTracker, FieldTracker())

    class Quotas(QuotaModelMixin.Quotas):
        test_quota = QuotaField(default_limit=1)

    @classmethod
    def get_url_name(cls):
        return "test-new-instances"

    @property
    def internal_ips(self):
        return ["127.0.0.1"]

    @property
    def external_ips(self):
        return ["8.8.8.8"]


class TestSubResource(structure_models.BaseResource):
    pass


class TestVolume(structure_models.Storage):
    pass


class TestSnapshot(structure_models.Storage):
    pass
