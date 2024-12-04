from django.utils.functional import cached_property

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace_remote.tests import factories


class MarketplaceRemoteFixture(marketplace_fixtures.MarketplaceFixture):
    def __init__(self):
        super().__init__()
        self.remote_local_category

    @cached_property
    def remote_synchronisation(self):
        return factories.RemoteSynchronisationFactory(
            local_service_provider=self.service_provider
        )

    @cached_property
    def remote_local_category(self):
        return factories.RemoteLocalCategoryFactory(
            local_category=marketplace_factories.CategoryFactory(),
            remote_synchronisation=self.remote_synchronisation,
        )
