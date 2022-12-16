from django.utils.functional import cached_property

from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.promotions.tests import factories


class PromotionsFixture(marketplace_fixtures.MarketplaceFixture):
    def __init__(self):
        super().__init__()
        self.discounted_resource

    @cached_property
    def campaign(self):
        campaign = factories.CampaignFactory(service_provider=self.service_provider)
        campaign.offerings.add(self.offering)
        campaign.save()
        return campaign

    @cached_property
    def discounted_resource(self):
        return factories.DiscountedResourceFactory(
            campaign=self.campaign,
            resource=self.resource,
        )
