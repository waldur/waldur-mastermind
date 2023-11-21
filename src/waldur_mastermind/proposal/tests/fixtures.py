from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.proposal.tests import factories as proposal_factories


class ProposalFixture(structure_fixtures.CustomerFixture):
    def __init__(self):
        self.call_manager

    @cached_property
    def call_manager(self):
        return proposal_factories.CallManagerFactory(
            customer=self.customer,
            description='CallManager\'s description',
        )
