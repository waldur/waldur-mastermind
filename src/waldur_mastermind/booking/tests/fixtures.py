from django.utils.functional import cached_property

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

from .. import PLUGIN_NAME


class BookingFixture(marketplace_fixtures.MarketplaceFixture):
    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, options={'order': []}, customer=self.customer
        )

    @cached_property
    def service_manager(self):
        user = structure_factories.UserFactory()
        self.customer.add_user(user, structure_models.CustomerRole.SERVICE_MANAGER)
        return user
