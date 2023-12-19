import factory

from waldur_core.structure.tests import factories as structure_factories

from .. import models


class PriceEstimateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.PriceEstimate

    scope = factory.SubFactory(structure_factories.ProjectFactory)
    total = factory.Iterator([10, 100, 1000, 10000, 980, 42])
