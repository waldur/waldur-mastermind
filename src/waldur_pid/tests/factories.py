import factory.fuzzy

from waldur_pid.tests import models


class OfferingFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Offering
