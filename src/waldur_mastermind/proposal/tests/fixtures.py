from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.tests import factories as proposal_factories


class ProposalFixture(structure_fixtures.CustomerFixture):
    def __init__(self):
        self.requested_offering
        self.new_call

    @cached_property
    def manager(self):
        return proposal_factories.ManagerFactory(
            customer=self.customer,
            description='Manager\'s description',
        )

    @cached_property
    def call(self):
        return proposal_factories.CallFactory(
            manager=self.manager,
            state=proposal_models.Call.States.ACTIVE,
            created_by=self.owner,
        )

    @cached_property
    def new_call(self):
        return proposal_factories.CallFactory(
            manager=self.manager, created_by=self.owner
        )

    @cached_property
    def offering_fixture(self):
        return marketplace_fixtures.MarketplaceFixture()

    @cached_property
    def requested_offering(self):
        return proposal_factories.RequestedOfferingFactory(
            call=self.call,
            state=proposal_models.RequestedOffering.States.REQUESTED,
            created_by=self.owner,
            offering=self.offering,
        )

    @cached_property
    def offering(self):
        return self.offering_fixture.offering

    @cached_property
    def offering_owner(self):
        return self.offering_fixture.offering_owner
