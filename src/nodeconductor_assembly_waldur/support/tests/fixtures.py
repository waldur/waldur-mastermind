from django.utils.functional import cached_property

from nodeconductor.structure.tests import fixtures as structure_fixtures

from . import factories


class SupportFixture(structure_fixtures.ServiceFixture):

    @cached_property
    def issue(self):
        return factories.IssueFactory(customer=self.customer, project=self.project)

    @cached_property
    def comment(self):
        return factories.CommentFactory(issue=self.issue)

    @cached_property
    def offering(self):
        return factories.OfferingFactory(issue=self.issue)
