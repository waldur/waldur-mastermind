import factory.fuzzy

from waldur_core.core.tests.types import BaseMetaFactory
from waldur_pid.tests import models


class OfferingFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Offering]
):
    class Meta:
        model = models.Offering
