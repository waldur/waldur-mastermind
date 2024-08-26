from django.utils.functional import cached_property

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.tests import factories as policy_factories


class OfferingUsagePolicyFixture(marketplace_fixtures.MarketplaceFixture):
    def __init__(self):
        self.component_limit
        self.organization_group

    @cached_property
    def policy(self):
        return policy_factories.OfferingUsagePolicyFactory(scope=self.offering)

    @cached_property
    def component_limit(self):
        return policy_factories.OfferingUsageComponentLimitFactory(
            policy=self.policy, component=self.offering_usage_component
        )

    @cached_property
    def organization_group(self):
        organization_group = structure_factories.OrganizationGroupFactory()
        self.policy.organization_groups.add(organization_group)
        return organization_group
