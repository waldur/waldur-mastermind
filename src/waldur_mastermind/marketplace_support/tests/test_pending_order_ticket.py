import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import override_settings

from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    BillingTypes,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests.base import BaseTest


@override_settings(task_always_eager=True)
class PendingOrderTicketTest(BaseTest):
    def _create_support_order(self, state=OrderStates.PENDING_CONSUMER, **kwargs):
        offering = kwargs.pop(
            "offering", None
        ) or marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING)
        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes={"name": "item_name", "description": "Description"},
            state=state,
            type=OrderTypes.CREATE,
            **kwargs,
        )
        return order

    def _get_issue_for_order(self, order):
        order_ct = ContentType.objects.get_for_model(order)
        return support_models.Issue.objects.filter(
            resource_object_id=order.id,
            resource_content_type=order_ct,
        )

    def test_support_order_entering_pending_start_date_creates_issue(self):
        order = self._create_support_order()
        order.state = OrderStates.PENDING_START_DATE
        order.save()

        issues = self._get_issue_for_order(order)
        self.assertEqual(issues.count(), 1)

    def test_support_order_entering_pending_project_creates_issue(self):
        order = self._create_support_order()
        order.state = OrderStates.PENDING_PROJECT
        order.save()

        issues = self._get_issue_for_order(order)
        self.assertEqual(issues.count(), 1)

    def test_non_support_order_entering_pending_start_date_does_not_create_issue(self):
        offering = marketplace_factories.OfferingFactory(type="Other.Offering")
        order = marketplace_factories.OrderFactory(
            offering=offering,
            attributes={"name": "item_name", "description": "Description"},
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.CREATE,
        )
        order.state = OrderStates.PENDING_START_DATE
        order.save()

        issues = self._get_issue_for_order(order)
        self.assertEqual(issues.count(), 0)

    def test_no_duplicate_issue_when_order_moves_to_executing(self):
        order = self._create_support_order()
        order.state = OrderStates.PENDING_START_DATE
        order.save()

        issues = self._get_issue_for_order(order)
        self.assertEqual(issues.count(), 1)

        # Simulate moving to EXECUTING -- create_issue guard should prevent duplicate
        order.state = OrderStates.EXECUTING
        order.save()

        # Still only one issue
        issues = self._get_issue_for_order(order)
        self.assertEqual(issues.count(), 1)

    def test_resource_scope_is_set_when_issue_created_for_pending_order(self):
        order = self._create_support_order()
        order.state = OrderStates.PENDING_START_DATE
        order.save()

        issues = self._get_issue_for_order(order)
        self.assertEqual(issues.count(), 1)
        issue = issues.first()

        order.resource.refresh_from_db()
        self.assertEqual(order.resource.scope, issue)

    def test_issue_not_created_on_order_creation(self):
        """Ensure the handler does not fire for newly created orders."""
        order = self._create_support_order(state=OrderStates.PENDING_START_DATE)
        issues = self._get_issue_for_order(order)
        self.assertEqual(issues.count(), 0)

    def test_issue_description_contains_slugs(self):
        order = self._create_support_order()
        order.state = OrderStates.PENDING_START_DATE
        order.save()

        issue = self._get_issue_for_order(order).first()
        self.assertIn(f"Resource slug: {order.resource.slug}", issue.description)
        self.assertIn(f"Project slug: {order.project.slug}", issue.description)
        self.assertIn(
            f"Customer slug: {order.project.customer.slug}", issue.description
        )

    def test_issue_description_contains_prepaid_total_cost(self):
        """Ticket includes full cost with duration multiplier for prepaid orders."""
        offering = marketplace_factories.OfferingFactory(type=SUPPORT_OFFERING)
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering,
            type="node_hours",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )
        plan = marketplace_factories.PlanFactory(offering=offering)
        marketplace_factories.PlanComponentFactory(
            plan=plan,
            component=component,
            price=Decimal("10"),
        )

        # 12-month prepaid order with 100 node_hours limit
        end_date = (datetime.date.today() + datetime.timedelta(days=365)).isoformat()
        order = marketplace_factories.OrderFactory(
            offering=offering,
            plan=plan,
            attributes={"name": "prepaid-test", "end_date": end_date},
            limits={"node_hours": 100},
            state=OrderStates.PENDING_CONSUMER,
            type=OrderTypes.CREATE,
        )
        order.resource.end_date = datetime.date.fromisoformat(end_date)
        order.resource.save()

        order.init_cost()
        order.save()

        order.state = OrderStates.PENDING_START_DATE
        order.save()

        issue = self._get_issue_for_order(order).first()
        self.assertIsNotNone(issue)
        self.assertIn("Total cost:", issue.description)
