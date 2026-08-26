"""Regression guards for defects found in `rebill_historical_usage`'s Cost
Policy preview (originally reproduced here as `xfail(strict=True)` tests
against the pre-fix implementation; now permanent assertions against the
fixed one -- see git history for the original xfail reasons).

Run with ``-s`` to print the full trace each test builds:

    DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local uv run pytest \
      src/waldur_mastermind/marketplace/tests/test_rebill_historical_usage_policy_preview.py -s
"""

import datetime
import decimal
import io
import re

from django.core.management import call_command
from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.compensations import MonthlyCompensation
from waldur_mastermind.marketplace import callbacks, models
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.policy import models as policy_models
from waldur_mastermind.policy import policy_actions


@freeze_time("2024-07-15")
class BasePolicyPreviewTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            plugin_options={"supports_pausing": True},
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering, billing_type=BillingTypes.USAGE, type="cpu"
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component, price=10
        )
        self.resource = self._create_resource()
        self.plan_period = models.ResourcePlanPeriod.objects.create(
            resource=self.resource, plan=self.plan
        )

    def _create_resource(self, state=ResourceStates.OK):
        resource = models.Resource.objects.create(
            offering=self.offering,
            plan=self.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        factories.OrderFactory(
            resource=resource,
            type=OrderTypes.CREATE,
            state=OrderStates.EXECUTING,
            plan=self.plan,
        )
        callbacks.resource_creation_succeeded(resource)
        if state != ResourceStates.OK:
            models.Resource.objects.filter(pk=resource.pk).update(state=state)
            resource.refresh_from_db()
        return resource

    def _bill_usage(self, year, month, amount):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "plan_period": self.plan_period.uuid.hex,
            "date": datetime.datetime(year, month, 15, tzinfo=datetime.UTC).isoformat(),
            "usages": [{"type": "cpu", "amount": amount}],
        }
        response = self.client.post(
            "/api/marketplace-component-usages/set_usage/", payload
        )
        assert response.status_code == 201, response.data

    def _create_stale_usage(self, year, month, old_amount, new_amount):
        """Bill usage, freeze the invoice, then correct the usage out-of-band --
        exactly the end state waldur_site_load_historical_usage leaves behind."""
        self._bill_usage(year, month, old_amount)
        invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer, year=year, month=month
        )
        invoice.set_created()
        models.ComponentUsage.objects.filter(
            resource=self.resource,
            component=self.offering_component,
            billing_period=datetime.date(year, month, 1),
        ).update(usage=new_amount)
        return invoice

    def _rebill(self, **kwargs):
        out = io.StringIO()
        call_command(
            "rebill_historical_usage",
            resource=self.resource.uuid.hex,
            stdout=out,
            **kwargs,
        )
        return out.getvalue()


class OfferingCostPolicyIsPreviewedTest(BasePolicyPreviewTest):
    """`_snapshot_cost_policies` collects only Project/Customer estimated cost
    policies. `OfferingEstimatedCostPolicy` is an `EstimatedCostPolicyMixin`
    too, with `trigger_class = InvoiceItem` wired to post_save in
    policy/apps.py, so the invoice items this command rewrites do trigger it.
    The command nonetheless reports a clean bill of health.
    """

    def test_offering_cost_policy_is_reported(self):
        self._create_stale_usage(2023, 12, old_amount=5, new_amount=80)
        policy = policy_models.OfferingEstimatedCostPolicy.objects.create(
            scope=self.offering,
            limit_cost=100,
            actions="block_creation_of_new_resources,notify_organization_owners",
            period=invoice_models.PeriodMixin.Periods.MONTH_12,
            apply_to_all=True,
            has_fired=False,
        )
        triggered_before = policy.is_triggered()
        output = self._rebill(execute=True)
        policy.refresh_from_db()
        triggered_after = policy.is_triggered()

        print("\n--- Offering cost policy preview ---")
        print("is_triggered() before correction:", triggered_before)
        print(output.rstrip())
        print("is_triggered() after correction: ", triggered_after)
        print(
            "actions that would run:",
            [action.method.__name__ for action in policy.get_all_actions()],
        )

        # The correction is what flips this policy on.
        self.assertFalse(triggered_before)
        self.assertTrue(triggered_after)
        # ...so the command must not claim there is nothing to evaluate.
        self.assertNotIn("no Cost Policy configured", output)
        self.assertIn(policy.uuid.hex, output)


class TerminateResourcesIsNotPreviewedAsResettableTest(BasePolicyPreviewTest):
    """`terminate_resources` has `reset_method=None`, so a fired -> clear
    transition runs nothing for it. The preview still announces that it
    "would reset on" the resources, which reads as though terminated
    resources get restored.
    """

    def test_terminate_resources_reset_is_not_announced(self):
        self._create_stale_usage(2023, 12, old_amount=50, new_amount=1)
        policy_models.ProjectEstimatedCostPolicy.objects.create(
            scope=self.fixture.project,
            limit_cost=100,
            actions="terminate_resources",
            use_credit=False,
            period=invoice_models.PeriodMixin.Periods.MONTH_12,
            has_fired=True,
        )
        output = self._rebill()

        print("\n--- terminate_resources reset preview ---")
        print(output.rstrip())
        print(
            "POLICY_ACTIONS['terminate_resources'].reset_method =",
            policy_actions.POLICY_ACTIONS["terminate_resources"].reset_method,
        )

        # Ground truth: nothing runs for this action on a reset.
        self.assertIsNone(
            policy_actions.POLICY_ACTIONS["terminate_resources"].reset_method
        )
        self.assertIn("WOULD RESET", output)  # the transition itself is real
        self.assertNotIn("terminate_resources would reset on", output)


class ReportedCostMatchesEvaluatedCostTest(BasePolicyPreviewTest):
    """`_report_policy_impact` subtracts the raw MonthlyCompensation figure,
    while `is_triggered()` subtracts it through `_pending_compensation`
    (policy/models.py). Compensations are ordinary invoice items with a
    negative unit_price, so the cost sum is already net of what is written --
    subtracting the projection wholesale deducts the same credit twice. The
    printed gate state can then contradict the verdict on the same line.
    """

    def test_reported_gate_state_agrees_with_verdict(self):
        # Historical usage billed and frozen before any credit exists, so the
        # frozen invoice carries no compensation item of its own. Large
        # enough that even after _apply_credit_correction now draws whatever
        # credit remains (see rebill_historical_usage.py) and leaves only the
        # true shortfall as cost, that shortfall alone still clears
        # limit_cost below -- this test is about gate/verdict agreement, not
        # about how much of the correction credit can cover.
        self._create_stale_usage(2023, 12, old_amount=5, new_amount=2000)

        # Ample credit plus current-month usage, compensated for real: this is
        # what writes the "already applied" credit items into the current month.
        credit = invoice_models.CustomerCredit.objects.create(
            customer=self.fixture.customer,
            value=decimal.Decimal("9000"),
            end_date=datetime.date(2030, 1, 1),
        )
        self._bill_usage(2024, 7, 500)
        current_invoice = invoice_models.Invoice.objects.get(
            customer=self.fixture.customer, year=2024, month=7
        )
        MonthlyCompensation(
            self.fixture.customer, invoice=current_invoice
        ).apply_compensations()
        credit.refresh_from_db()

        policy = policy_models.ProjectEstimatedCostPolicy.objects.create(
            scope=self.fixture.project,
            limit_cost=10000,
            actions="request_pausing",
            use_credit=True,
            period=invoice_models.PeriodMixin.Periods.MONTH_12,
            has_fired=False,
        )
        raw_projection = MonthlyCompensation(
            self.fixture.customer
        ).get_project_compensation(self.fixture.project)
        _items, real_deduction = policy._cost_inputs()
        output = self._rebill()

        print("\n--- reported cost vs evaluated cost ---")
        print("credit balance after real compensation:", credit.value)
        print("raw projection (what the preview subtracts):  ", raw_projection)
        print("_pending_compensation (what is_triggered uses):", real_deduction)
        print(
            "preview understates cost by:",
            decimal.Decimal(raw_projection) - decimal.Decimal(real_deduction),
        )
        print(output.rstrip())

        self.assertIn("WOULD FIRE", output)
        # A policy that is about to fire cannot have a closed gate 1.
        self.assertNotIn("gate 1: closed", output)


class GateBoundaryMatchesIsTriggeredTest(BasePolicyPreviewTest):
    """The preview calls gate 1 open on `net_cost >= limit_cost`;
    `EstimatedCostPolicyMixin._is_triggered` uses `>`. They disagree when the
    cost lands exactly on the limit.
    """

    def test_cost_exactly_on_limit_reports_gate_closed(self):
        # 10 units * $10 = $100, against limit_cost = 100.
        self._create_stale_usage(2023, 12, old_amount=1, new_amount=10)
        policy_models.ProjectEstimatedCostPolicy.objects.create(
            scope=self.fixture.project,
            limit_cost=100,
            actions="request_pausing",
            use_credit=False,
            period=invoice_models.PeriodMixin.Periods.MONTH_12,
            has_fired=False,
        )
        output = self._rebill()

        print("\n--- gate boundary ---")
        print(output.rstrip())
        print("_is_triggered uses `>`:", 100 > 100)
        print("preview uses `>=`:     ", 100 >= 100)

        self.assertIn("cost_this_window=100.0000", output)
        self.assertIn("fired: False -> False", output)
        # Not triggered, so the gate it reports must read closed.
        self.assertIn("(gate 1: closed)", output)


class AffectedResourceListMatchesActionTest(BasePolicyPreviewTest):
    """The preview lists candidate resources without the two filters the real
    action applies: `_apply_generic_action` skips resources whose flag already
    holds the target value, and `request_pausing` excludes only TERMINATED
    (not TERMINATING) resources. So the list both over- and under-reports.
    """

    def test_previewed_resources_match_what_the_action_touches(self):
        # Already paused -- _apply_generic_action skips it.
        self.resource.paused = True
        self.resource.save(update_fields=["paused"])
        # TERMINATING sibling -- request_pausing excludes only TERMINATED.
        terminating = self._create_resource(state=ResourceStates.TERMINATING)

        self._create_stale_usage(2023, 12, old_amount=1, new_amount=50)
        policy = policy_models.ProjectEstimatedCostPolicy.objects.create(
            scope=self.fixture.project,
            limit_cost=100,
            actions="request_pausing",
            use_credit=False,
            period=invoice_models.PeriodMixin.Periods.MONTH_12,
            has_fired=False,
        )
        output = self._rebill()

        # Ground truth: run the real action and see what it actually changes.
        policy.refresh_from_db()
        policy_actions.request_pausing(policy)
        self.resource.refresh_from_db()
        terminating.refresh_from_db()

        print("\n--- previewed resources vs what the action touches ---")
        print(output.rstrip())
        print("after the real request_pausing():")
        print(
            f"  corrected resource {self.resource.uuid.hex} paused="
            f"{self.resource.paused} (already paused, so the action skipped it)"
        )
        print(
            f"  TERMINATING sibling {terminating.uuid.hex} paused="
            f"{terminating.paused} (the action did pause it)"
        )

        self.assertIn("request_pausing would apply to", output)
        listed = output.split("request_pausing would apply to")[1]
        # The action skips the already-paused resource, so it must not be listed.
        self.assertNotIn(self.resource.uuid.hex, listed)
        # The action does pause the TERMINATING one, so it must be listed.
        self.assertTrue(terminating.paused)
        self.assertIn(terminating.uuid.hex, listed)


class MultiPeriodDryRunAccumulatesTest(BasePolicyPreviewTest):
    """Each resource-period used to run in (and immediately roll back) its
    own transaction, so a dry run of period N's Cost Policy preview never saw
    period 1..N-1's corrections -- even though they're part of the same dry
    run and --execute commits them in order, so period N's real evaluation
    DOES see them. A dry run of several periods for the same resource
    therefore understated cost_this_window for every period after the first.
    """

    def test_second_period_preview_includes_first_periods_correction(self):
        # Both periods fall inside the MONTH_12 window anchored on the frozen
        # "today" (2024-07-15: Aug 2023 .. Jul 2024), so both should count
        # toward cost_this_window by the time the second period is evaluated.
        self._create_stale_usage(2023, 12, old_amount=1, new_amount=10)
        self._create_stale_usage(2024, 1, old_amount=1, new_amount=5)
        policy = policy_models.ProjectEstimatedCostPolicy.objects.create(
            scope=self.fixture.project,
            limit_cost=1000,
            actions="request_pausing",
            use_credit=False,
            period=invoice_models.PeriodMixin.Periods.MONTH_12,
            has_fired=False,
        )

        output = self._rebill()
        cost_windows = [
            decimal.Decimal(value)
            for value in re.findall(r"cost_this_window=([\d.]+)", output)
        ]
        self.assertEqual(len(cost_windows), 2)
        first_period_cost, second_period_cost = cost_windows

        # Ground truth: actually apply both corrections for real, then ask
        # the policy itself (the same _cost_inputs/_evaluated_cost the
        # preview calls) what the cumulative window cost is.
        self._rebill(execute=True)
        policy.refresh_from_db()
        real_invoice_items, real_deduction = policy._cost_inputs()
        real_cost = policy._evaluated_cost(real_invoice_items, real_deduction)

        print("\n--- multi-period dry run accumulation ---")
        print(output.rstrip())
        print("ground truth cost_this_window after --execute:", real_cost)

        self.assertEqual(second_period_cost, real_cost)
        # The first period's own preview runs before the second period's
        # correction exists, so it can't already include that money.
        self.assertLess(first_period_cost, second_period_cost)


class CreditExhaustionOnlyCountsTheUncoveredOverageTest(BasePolicyPreviewTest):
    """When a period's corrected cost exceeds available credit,
    _apply_credit_correction used to abort the whole credit correction,
    leaving that period's ENTIRE incurred cost uncompensated instead of just
    the genuine shortfall. cost_this_window then jumped by the full period
    cost the moment credit ran out, rather than by the true overage -- firing
    a Cost Policy far earlier than the customer's actual credit exhaustion
    warrants. Reproduces a customer-reported scenario with a hand-derived
    expected cumulative overage.
    """

    def test_cost_this_window_is_the_cumulative_uncovered_overage(self):
        invoice_models.CustomerCredit.objects.create(
            customer=self.fixture.customer,
            value=decimal.Decimal("60"),
            end_date=datetime.date(2030, 1, 1),
        )
        # $10/unit (setUp). Period 1's $50 is fully covered by the $60
        # credit, leaving $10 available. Period 2's $80 can only draw that
        # remaining $10, leaving $70 uncovered. Period 3's $60 draws nothing
        # (credit is now fully exhausted), so all $60 is uncovered.
        # Cumulative uncovered overage = 0 + 70 + 60 = 130.
        self._create_stale_usage(2023, 12, old_amount=1, new_amount=5)  # $50
        self._create_stale_usage(2024, 1, old_amount=1, new_amount=8)  # $80
        self._create_stale_usage(2024, 2, old_amount=1, new_amount=6)  # $60
        policy_models.ProjectEstimatedCostPolicy.objects.create(
            scope=self.fixture.project,
            limit_cost=100,
            actions="request_pausing",
            use_credit=False,
            period=invoice_models.PeriodMixin.Periods.MONTH_12,
            has_fired=False,
        )

        output = self._rebill()
        cost_windows = [
            decimal.Decimal(value)
            for value in re.findall(r"cost_this_window=([\d.]+)", output)
        ]
        self.assertEqual(len(cost_windows), 3)

        print("\n--- credit exhaustion: cumulative uncovered overage ---")
        print(output.rstrip())

        self.assertEqual(cost_windows[-1], decimal.Decimal("130"))
        self.assertIn("WOULD FIRE", output)
