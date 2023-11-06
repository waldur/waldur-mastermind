import factory

from waldur_core.quotas.models import QuotaUsage


class QuotaFactory(factory.DjangoModelFactory):
    class Meta:
        model = QuotaUsage

    name = factory.Sequence(lambda i: 'quota_%s' % i)
