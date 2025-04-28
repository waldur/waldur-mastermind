import factory

from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.quotas.models import QuotaUsage


class QuotaFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[QuotaUsage]
):
    class Meta:
        model = QuotaUsage

    name = factory.Sequence(lambda i: "quota_%s" % i)
