import datetime
from decimal import Decimal
from unittest import mock

from constance.test.unittest import override_config
from django.utils import timezone
from rest_framework import test

from waldur_core.core.middleware import skip_side_effects
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models, order_approval
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.tests import factories


def _attach_components(offering, specs):
    for component_type, billing_type in specs:
        factories.OfferingComponentFactory(
            offering=offering, type=component_type, billing_type=billing_type
        )


def _make_order_quietly(**kwargs):
    """Create an order with all post-save side effects suppressed."""
    with skip_side_effects():
        return factories.OrderFactory(**kwargs)


class EligibilityHelperTest(test.APITestCase):
    """Pure-function tests for evaluate_auto_approval — no signal handling."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.owner = self.fixture.owner
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        _attach_components(self.offering, [("cpu", BillingTypes.FIXED)])
        self.plan = factories.PlanFactory(
            offering=self.offering, unit_price=Decimal("50")
        )

    def _make_rule(self, limit="100", enabled=True, created_by=None):
        return models.ProjectOrderAutoApproval.objects.create(
            project=self.fixture.project,
            monthly_cost_limit=Decimal(limit),
            enabled=enabled,
            created_by=created_by or self.owner,
        )

    def _make_order(self, **kwargs):
        kwargs.setdefault("project", self.fixture.project)
        kwargs.setdefault("offering", self.offering)
        kwargs.setdefault("plan", self.plan)
        kwargs.setdefault("created_by", self.fixture.user)
        return _make_order_quietly(**kwargs)

    def test_under_limit_returns_rule(self):
        rule = self._make_rule(limit="100")
        order = self._make_order()
        self.assertEqual(order_approval.evaluate_auto_approval(order), rule)

    def test_at_limit_is_inclusive(self):
        rule = self._make_rule(limit="50")
        order = self._make_order()
        self.assertEqual(order_approval.evaluate_auto_approval(order), rule)

    def test_over_limit_returns_none(self):
        self._make_rule(limit="10")
        order = self._make_order()
        self.assertIsNone(order_approval.evaluate_auto_approval(order))

    def test_usage_component_disqualifies(self):
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        _attach_components(
            offering,
            [("cpu", BillingTypes.FIXED), ("storage", BillingTypes.USAGE)],
        )
        plan = factories.PlanFactory(offering=offering, unit_price=Decimal("5"))
        self._make_rule(limit="9999")
        order = self._make_order(offering=offering, plan=plan)
        self.assertIsNone(order_approval.evaluate_auto_approval(order))

    def test_limit_component_is_predictable(self):
        offering = factories.OfferingFactory(customer=self.fixture.customer)
        _attach_components(
            offering,
            [("cpu", BillingTypes.FIXED), ("storage", BillingTypes.LIMIT)],
        )
        plan = factories.PlanFactory(offering=offering, unit_price=Decimal("10"))
        rule = self._make_rule(limit="100")
        order = self._make_order(offering=offering, plan=plan)
        self.assertEqual(order_approval.evaluate_auto_approval(order), rule)

    def test_no_rule_returns_none(self):
        order = self._make_order()
        self.assertIsNone(order_approval.evaluate_auto_approval(order))

    def test_disabled_rule_returns_none(self):
        self._make_rule(limit="100", enabled=False)
        order = self._make_order()
        self.assertIsNone(order_approval.evaluate_auto_approval(order))

    def test_non_pending_consumer_state_returns_none(self):
        self._make_rule(limit="100")
        order = self._make_order()
        order.state = OrderStates.EXECUTING
        order.save(update_fields=["state"])
        self.assertIsNone(order_approval.evaluate_auto_approval(order))

    def test_terminate_order_treated_as_zero_cost(self):
        # Plan unit_price is huge, but TERMINATE returns 0 → always under limit.
        expensive = factories.PlanFactory(
            offering=self.offering, unit_price=Decimal("9999")
        )
        rule = self._make_rule(limit="1")
        order = self._make_order(plan=expensive, type=OrderTypes.TERMINATE)
        self.assertEqual(order_approval.evaluate_auto_approval(order), rule)

    def test_terminate_order_exempt_from_purchase_order_requirement(self):
        self.offering.plugin_options = {"require_purchase_order_upload": True}
        self.offering.save()
        rule = self._make_rule(limit="9999")
        order = self._make_order(type=OrderTypes.TERMINATE)
        self.assertEqual(order_approval.evaluate_auto_approval(order), rule)

    def test_require_purchase_order_blocks_auto_approval(self):
        self.offering.plugin_options = {"require_purchase_order_upload": True}
        self.offering.save()
        self._make_rule(limit="9999")
        order = self._make_order()
        self.assertIsNone(order_approval.evaluate_auto_approval(order))

    def test_require_purchase_order_with_attachment_allows_auto_approval(self):
        self.offering.plugin_options = {"require_purchase_order_upload": True}
        self.offering.save()
        rule = self._make_rule(limit="9999")
        order = self._make_order()
        order.attachment = "marketplace_order_attachments/po.pdf"
        order.save(update_fields=["attachment"])
        self.assertEqual(order_approval.evaluate_auto_approval(order), rule)


class EstimatedMonthlyCostTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        _attach_components(self.offering, [("cpu", BillingTypes.FIXED)])
        self.plan = factories.PlanFactory(
            offering=self.offering, unit_price=Decimal("42")
        )

    def test_zero_for_terminate(self):
        order = _make_order_quietly(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            type=OrderTypes.TERMINATE,
        )
        self.assertEqual(order_approval.estimated_monthly_cost(order), Decimal("0"))

    def test_unit_price_for_create(self):
        order = _make_order_quietly(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            type=OrderTypes.CREATE,
        )
        self.assertEqual(order_approval.estimated_monthly_cost(order), Decimal("42"))


class StaleCreatorTest(test.APITestCase):
    """Defense-in-depth: rule whose created_by lost APPROVE_ORDER must not fire."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        _attach_components(self.offering, [("cpu", BillingTypes.FIXED)])
        self.plan = factories.PlanFactory(
            offering=self.offering, unit_price=Decimal("10")
        )

    def _make_order(self):
        return factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            created_by=self.fixture.user,
        )

    def test_creator_without_permission_skips_and_logs(self):
        random_user = self.fixture.member  # no APPROVE_ORDER anywhere
        models.ProjectOrderAutoApproval.objects.create(
            project=self.fixture.project,
            monthly_cost_limit=Decimal("100"),
            created_by=random_user,
        )
        order = self._make_order()
        with self.assertLogs(
            "waldur_mastermind.marketplace.order_approval", level="WARNING"
        ) as cm:
            applied = order_approval.try_apply_project_auto_approval(order.pk)
        self.assertFalse(applied)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_CONSUMER)
        self.assertTrue(any("lost APPROVE_ORDER" in m for m in cm.output))

    def test_staff_creator_passes_re_check(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        staff = self.fixture.staff
        rule = models.ProjectOrderAutoApproval.objects.create(
            project=self.fixture.project,
            monthly_cost_limit=Decimal("100"),
            created_by=staff,
        )
        self._make_order()
        self.assertTrue(order_approval._rule_creator_can_still_approve(rule))


class TryApplyTransitionTest(test.APITestCase):
    """Exercise try_apply_project_auto_approval directly (no signal involvement)."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.owner = self.fixture.owner
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        _attach_components(self.offering, [("cpu", BillingTypes.FIXED)])
        self.plan = factories.PlanFactory(
            offering=self.offering, unit_price=Decimal("10")
        )

    def _make_rule(self, limit="100"):
        return models.ProjectOrderAutoApproval.objects.create(
            project=self.fixture.project,
            monthly_cost_limit=Decimal(limit),
            created_by=self.owner,
        )

    def _make_order(self, **kwargs):
        kwargs.setdefault("project", self.fixture.project)
        kwargs.setdefault("offering", self.offering)
        kwargs.setdefault("plan", self.plan)
        kwargs.setdefault("created_by", self.fixture.user)
        return _make_order_quietly(**kwargs)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay")
    def test_under_limit_transitions_to_executing(self, mocked_delay):
        rule = self._make_rule(limit="100")
        order = self._make_order()
        applied = order_approval.try_apply_project_auto_approval(order.pk)
        self.assertTrue(applied)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.auto_approved_by_rule_id, rule.id)
        self.assertEqual(order.auto_approved_cost_limit_snapshot, Decimal("100"))
        self.assertEqual(order.consumer_reviewed_by, self.owner)
        mocked_delay.assert_called()

    def test_over_limit_does_not_transition(self):
        self._make_rule(limit="1")
        order = self._make_order()
        applied = order_approval.try_apply_project_auto_approval(order.pk)
        self.assertFalse(applied)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_CONSUMER)
        self.assertIsNone(order.auto_approved_by_rule_id)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay")
    def test_routes_to_pending_provider(self, _):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer, type=BASIC_OFFERING
        )
        _attach_components(offering, [("cpu", BillingTypes.FIXED)])
        plan = factories.PlanFactory(offering=offering, unit_price=Decimal("5"))
        self._make_rule(limit="100")
        order = self._make_order(offering=offering, plan=plan)
        applied = order_approval.try_apply_project_auto_approval(order.pk)
        self.assertTrue(applied)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_PROVIDER)
        self.assertIsNotNone(order.auto_approved_by_rule_id)

    def test_routes_to_pending_project_when_project_start_date_in_future(self):
        self.fixture.project.start_date = datetime.date(year=2099, month=1, day=1)
        self.fixture.project.save()
        self._make_rule(limit="100")
        order = self._make_order()
        applied = order_approval.try_apply_project_auto_approval(order.pk)
        self.assertTrue(applied)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_PROJECT)

    @mock.patch(
        "waldur_mastermind.marketplace.utils.order_should_not_be_reviewed_by_provider",
        return_value=True,
    )
    @override_config(ENABLE_ORDER_START_DATE=True)
    @mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay")
    def test_routes_to_pending_start_date(self, _delay, _provider_skip):
        self._make_rule(limit="100")
        future = (timezone.now() + datetime.timedelta(days=10)).date()
        order = self._make_order(start_date=future)
        applied = order_approval.try_apply_project_auto_approval(order.pk)
        self.assertTrue(applied)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.PENDING_START_DATE)

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay")
    def test_terminate_unconditional_auto_approval(self, mocked_delay):
        expensive = factories.PlanFactory(
            offering=self.offering, unit_price=Decimal("9999")
        )
        self._make_rule(limit="1")
        order = self._make_order(plan=expensive, type=OrderTypes.TERMINATE)
        applied = order_approval.try_apply_project_auto_approval(order.pk)
        self.assertTrue(applied)
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.EXECUTING)

    def test_already_approved_order_is_skipped(self):
        self._make_rule(limit="100")
        order = self._make_order()
        order.state = OrderStates.EXECUTING
        order.save(update_fields=["state"])
        applied = order_approval.try_apply_project_auto_approval(order.pk)
        self.assertFalse(applied)


class SignalIntegrationTest(test.APITransactionTestCase):
    """End-to-end: creating a qualifying order through the signal path."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        self.owner = self.fixture.owner
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        _attach_components(self.offering, [("cpu", BillingTypes.FIXED)])
        self.plan = factories.PlanFactory(
            offering=self.offering, unit_price=Decimal("10")
        )

    @mock.patch("waldur_mastermind.marketplace.tasks.process_order.delay")
    def test_order_creation_triggers_auto_approval(self, mocked_delay):
        rule = models.ProjectOrderAutoApproval.objects.create(
            project=self.fixture.project,
            monthly_cost_limit=Decimal("100"),
            created_by=self.owner,
        )
        order = factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            created_by=self.fixture.user,
        )
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.auto_approved_by_rule_id, rule.id)
