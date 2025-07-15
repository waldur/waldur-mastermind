from django.utils.functional import cached_property

from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class AutoprovisioningFixture(marketplace_fixtures.MarketplaceFixture):
    def __init__(self):
        super().__init__()

    @cached_property
    def rule(self):
        return autoprovisioning_factories.RuleFactory(plan=self.plan)
