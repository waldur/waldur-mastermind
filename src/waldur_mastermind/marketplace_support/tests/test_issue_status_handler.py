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
            name='Completed', type=support_models.IssueStatus.Types.RESOLVED,
        )

    @cached_property
    def fail_issue_status(self):
        return support_factories.IssueStatusFactory(
            name='Cancelled', type=support_models.IssueStatus.Types.CANCELED,
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
        return support_factories.IssueFactory(resource=self.order_item)

    @cached_property
    def order(self):
        return marketplace_factories.OrderFactory(
            state=marketplace_models.Order.States.EXECUTING, project=self.project
        )

    @cached_property
    def order_item(self):
        return marketplace_factories.OrderItemFactory(
            state=marketplace_models.OrderItem.States.EXECUTING,
            offering=self.offering,
            resource=self.resource,
            order=self.order,
        )


class IssueStatusHandlerTest(BaseTest):
    def setUp(self):
        super(IssueStatusHandlerTest, self).setUp()
        self.fixture = SupportFixture()

    def test_order_item_is_done_when_resource_creation_issue_is_resolved(self):
        self.fixture.issue.status = self.fixture.success_issue_status.name
        self.fixture.issue.save()

        self.fixture.order_item.refresh_from_db()
        self.assertEqual(
            self.fixture.order_item.state, self.fixture.order_item.States.DONE
        )

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.OK
        )

        self.fixture.order.refresh_from_db()
        self.assertEqual(self.fixture.order.state, marketplace_models.Order.States.DONE)

    def test_order_item_is_erred_when_resource_creation_issue_is_failed(self):
        self.fixture.issue.status = self.fixture.fail_issue_status.name
        self.fixture.issue.save()

        self.fixture.order_item.refresh_from_db()
        self.assertEqual(
            self.fixture.order_item.state, marketplace_models.OrderItem.States.ERRED
        )

        self.fixture.resource.refresh_from_db()
        self.assertEqual(
            self.fixture.resource.state, marketplace_models.Resource.States.ERRED
        )

        self.fixture.order.refresh_from_db()
        self.assertEqual(self.fixture.order.state, marketplace_models.Order.States.DONE)
