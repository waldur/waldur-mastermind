from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures

from . import factories


class SupportFixture(structure_fixtures.ServiceFixture):
    @cached_property
    def issue(self):
        issue = factories.IssueFactory(customer=self.customer, project=self.project)
        factories.SupportCustomerFactory(user=issue.caller)
        return issue

    @cached_property
    def comment(self):
        return factories.CommentFactory(issue=self.issue)
