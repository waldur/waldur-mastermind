from django.utils.functional import cached_property

from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

from ...marketplace.enums import BOOKING_OFFERING


class BookingFixture(marketplace_fixtures.MarketplaceFixture):
    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=BOOKING_OFFERING,
            state=OfferingStates.ACTIVE,
        )

    @cached_property
    def offering_service_manager(self):
        user = structure_factories.UserFactory(
            first_name="Service", last_name="Manager"
        )
        self.offering.customer.add_user(user, ServiceProviderRole.MANAGER)
        return user

    @cached_property
    def offering_owner(self):
        user = structure_factories.UserFactory()
        self.offering.customer.add_user(user, CustomerRole.OWNER)
        return user
