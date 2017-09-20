from django.utils.functional import cached_property

from nodeconductor.structure.tests import fixtures as structure_fixtures
from nodeconductor_assembly_waldur.support.tests import factories as support_factories

from . import factories


class ExpertsFixture(structure_fixtures.ServiceFixture):

    @cached_property
    def contract(self):
        return factories.ExpertContractFactory(team=self.project, request=self.expert_request)

    @cached_property
    def expert_request(self):
        expert_request = factories.ExpertRequestFactory(
            project=self.project,
            issue=support_factories.IssueFactory(customer=self.customer, project=self.project),
            user=self.user)
        return expert_request

