from functools import cached_property

from waldur_core.structure.tests.factories import ProjectFactory
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories
from waldur_mastermind.support.tests.base import BaseTest


class SupportFixture:
    def __init__(self):
        self.success_issue_status
        self.fail_issue_status

    @cached_property
    def success_issue_status(self):
        return support_factories.IssueStatusFactory(
            name='Completed',
            type=support_models.IssueStatus.Types.RESOLVED,
        )

    @cached_property
    def second_success_issue_status(self):
        return support_factories.IssueStatusFactory(
            name='Done',
            type=support_models.IssueStatus.Types.RESOLVED,
        )

    @cached_property
    def fail_issue_status(self):
        return support_factories.IssueStatusFactory(
            name='Cancelled',
            type=support_models.IssueStatus.Types.CANCELED,
        )

    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(type=PLUGIN_NAME)

    @cached_property
    def project(self):
        return ProjectFactory()

    @cached_property
    def resource(self):
        return marketplace_factories.ResourceFactory(
            offering=self.offering, project=self.project
        )

    @cached_property
    def issue(self):
        return support_factories.IssueFactory(resource=self.order)

    @cached_property
    def order(self):
        return marketplace_factories.OrderFactory(
            project=self.project,
            state=marketplace_models.Order.States.EXECUTING,
            offering=self.offering,
            resource=self.resource,
        )


class IssueStatusHandlerTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.fixture = SupportFixture()

    def test_order_is_done_when_resource_creation_issue_is_resolved(self):
        self.fixture.issue.status = self.fixture.success_issue_status.name
        self.fixture.issue.save()

        self.fixture.order.refresh_from_db()
        self.assertEqual(self.fixture.order.state, marketplace_models.Order.States.DONE)

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.OK
        )

    def test_order_is_terminated_when_resource_creation_issue_is_canceled(self):
        self.fixture.issue.status = self.fixture.fail_issue_status.name
        self.fixture.issue.save()

        self.fixture.order.refresh_from_db()
        self.assertEqual(
            self.fixture.order.state,
            marketplace_models.Order.States.CANCELED,
        )

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.TERMINATED
        )

    def test_use_second_resolve_state(self):
        self.fixture.issue.status = self.fixture.success_issue_status.name
        self.fixture.issue.save()

        self.fixture.order.refresh_from_db()
        self.assertEqual(self.fixture.order.state, marketplace_models.Order.States.DONE)

        self.fixture.issue.status = self.fixture.second_success_issue_status.name
        self.fixture.issue.save()
