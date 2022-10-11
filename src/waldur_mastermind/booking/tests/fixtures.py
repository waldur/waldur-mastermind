from django.utils.functional import cached_property

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

from .. import PLUGIN_NAME


class BookingFixture(marketplace_fixtures.MarketplaceFixture):
    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            options={'order': []},
            state=marketplace_models.Offering.States.ACTIVE,
        )

    @cached_property
    def offering_service_manager(self):
        user = structure_factories.UserFactory(
            first_name='Service', last_name='Manager'
        )
        self.offering.customer.add_user(
            user, structure_models.CustomerRole.SERVICE_MANAGER
        )
        return user

    @cached_property
    def offering_owner(self):
        user = structure_factories.UserFactory()
        self.offering.customer.add_user(user, structure_models.CustomerRole.OWNER)
        return user
