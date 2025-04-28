import factory

from waldur_autoprovisioning import models as autoprovisioning_models
from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class RuleFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[autoprovisioning_models.Rule],
):
    class Meta:
        model = autoprovisioning_models.Rule

    user_affiliations = factory.LazyFunction(list)
    user_email_patterns = factory.LazyFunction(list)
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class RulePlansFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[autoprovisioning_models.RulePlans],
):
    class Meta:
        model = autoprovisioning_models.RulePlans

    plan = factory.SubFactory(marketplace_factories.PlanFactory)
    rule = factory.SubFactory(RuleFactory)
