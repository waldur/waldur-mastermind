from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import backend
from . import factories


class SupportFixture(structure_fixtures.ServiceFixture):
    def __int__(self):
        self.support_user
        self.feedback
        self.comment

    @cached_property
    def backend_name(self):
        return backend.get_active_backend().backend_name

    @cached_property
    def issue(self):
        issue = factories.IssueFactory(
            customer=self.customer, project=self.project, backend_name=self.backend_name
        )
        factories.SupportCustomerFactory(user=issue.caller)
        return issue

    @cached_property
    def support_user(self):
        return factories.SupportUserFactory(
            user=self.issue.caller, backend_name=self.backend_name
        )

    @cached_property
    def comment(self):
        return factories.CommentFactory(
            issue=self.issue, backend_name=self.backend_name
        )

    @cached_property
    def attachment(self):
        return factories.AttachmentFactory(
            issue=self.issue, backend_name=self.backend_name
        )

    @cached_property
    def feedback(self):
        return factories.FeedbackFactory(issue=self.issue)
