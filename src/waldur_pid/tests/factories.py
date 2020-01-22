import factory.fuzzy

from . import models


class OfferingFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Offering
