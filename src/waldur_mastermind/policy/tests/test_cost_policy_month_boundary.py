"""
Tests for cost policy evaluation debouncing at month boundaries.

At month boundaries, the site agent creates many invoice items rapidly.
Each InvoiceItem.post_save dispatches a Celery task that evaluates
cost policies. MonthlyCompensation depends on the full set of invoice
items to correctly distribute shared credit across projects. Evaluating
during the burst — when only some items exist — produces non-deterministic
results that can cause false policy triggers.

The debounce fix delays cost policy evaluation by COST_POLICY_DEBOUNCE_SECONDS
so it runs after the burst settles, when all items are present.
"""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.handlers import (
    _CUSTOMER_COMPONENT_USAGE_POLICY_PATH,
    _CUSTOMER_POLICY_PATH,
    _OFFERING_ESTIMATED_COST_POLICY_PATH,
    _OFFERING_USAGE_POLICY_PATH,
    COST_POLICY_DEBOUNCE_SECONDS,
    _debounced_evaluate,
)
from waldur_mastermind.policy.tests import factories as policy_factories


@override_settings(WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
@freeze_time("2026-04-01")
class CostPolicyDebounceTest(test.APITestCase):
    """Tests that cost policy signal handlers debounce rapid evaluations."""

    def setUp(self):
        cache.clear()

    def test_debounced_evaluate_schedules_single_task(self):
        """Multiple calls with the same cache key should schedule only one task."""
        with mock.patch(
            "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
        ) as mock_apply:
            for _ in range(5):
                _debounced_evaluate(
                    "test.PolicyPath",
                    {"scope_id": 42},
                    "test_debounce_key",
                )

            self.assertEqual(
                mock_apply.call_count,
                1,
                f"Expected 1 task, got {mock_apply.call_count}.",
            )
            self.assertEqual(
                mock_apply.call_args.kwargs["countdown"],
                COST_POLICY_DEBOUNCE_SECONDS,
            )

    def test_different_cache_keys_schedule_separate_tasks(self):
        """Different cache keys should each get their own task."""
        with mock.patch(
            "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
        ) as mock_apply:
            _debounced_evaluate("test.Path", {"scope_id": 1}, "key_project_1")
            _debounced_evaluate("test.Path", {"scope_id": 2}, "key_project_2")
            _debounced_evaluate("test.Path", {"scope_id": 3}, "key_project_3")

            self.assertEqual(mock_apply.call_count, 3)

    def test_same_key_different_filters_still_deduplicates(self):
        """Cache key determines deduplication, not the filters."""
        with mock.patch(
            "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
        ) as mock_apply:
            _debounced_evaluate("test.Path", {"scope_id": 1}, "same_key")
            _debounced_evaluate("test.Path", {"scope_id": 2}, "same_key")

            self.assertEqual(mock_apply.call_count, 1)

    def test_cache_expiry_allows_new_task(self):
        """After cache key expires, a new task can be scheduled."""
        with mock.patch(
            "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
        ) as mock_apply:
            _debounced_evaluate("test.Path", {"scope_id": 1}, "expiry_key")
            self.assertEqual(mock_apply.call_count, 1)

            # Simulate cache expiry
            cache.delete("expiry_key")

            _debounced_evaluate("test.Path", {"scope_id": 1}, "expiry_key")
            self.assertEqual(mock_apply.call_count, 2)


@override_settings(task_always_eager=True, WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
@freeze_time("2026-04-01")
class CostPolicyHandlerIntegrationTest(test.APITestCase):
    """Integration test: verify that InvoiceItem saves trigger debounced
    evaluation via the actual signal handlers."""

    def setUp(self):
        cache.clear()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.resource = self.fixture.resource
        self.resource.state = 2
        self.resource.save()

        self.invoice = invoices_models.Invoice.objects.get(
            customer=self.customer,
            month=4,
            year=2026,
        )
        self.invoice.tax_percent = 0
        self.invoice.save()

    def test_invoice_item_saves_use_debounced_evaluate(self):
        """Verify that InvoiceItem.post_save triggers _debounced_evaluate
        (not direct .delay()) for cost policy handlers."""
        with mock.patch(
            "waldur_mastermind.policy.handlers._debounced_evaluate"
        ) as mock_debounce:
            invoices_factories.InvoiceItemFactory(
                invoice=self.invoice,
                project=self.project,
                unit_price=Decimal("10"),
                quantity=1,
                resource=self.resource,
            )

            # The project and customer cost policy handlers should both
            # call _debounced_evaluate
            debounce_calls = [
                c
                for c in mock_debounce.call_args_list
                if "EstimatedCostPolicy" in c.args[0]
            ]
            self.assertGreaterEqual(
                len(debounce_calls),
                2,
                "Both project and customer cost policy handlers should use "
                f"_debounced_evaluate. Got calls: {mock_debounce.call_args_list}",
            )

    def test_rapid_saves_debounce_to_three_tasks(self):
        """5 rapid InvoiceItem saves should result in 3 scheduled tasks
        (one per unique debounce key: project + customer + offering), even
        though _debounced_evaluate is called many times."""
        with mock.patch(
            "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
        ) as mock_apply:
            cache.clear()  # Ensure clean state
            for _ in range(5):
                invoices_factories.InvoiceItemFactory(
                    invoice=self.invoice,
                    project=self.project,
                    unit_price=Decimal("10"),
                    quantity=1,
                    resource=self.resource,
                )

            debounced_calls = [
                c
                for c in mock_apply.call_args_list
                if c.kwargs.get("countdown") == COST_POLICY_DEBOUNCE_SECONDS
            ]
            scheduled_paths = {c.kwargs["args"][0] for c in debounced_calls}
            self.assertEqual(
                len(debounced_calls),
                3,
                f"Expected 3 debounced tasks (project + customer + offering), "
                f"got {len(debounced_calls)}: {scheduled_paths}",
            )
            self.assertIn(_OFFERING_ESTIMATED_COST_POLICY_PATH, scheduled_paths)


def _publishes_for(apply_async_mock, delay_mock, policy_path):
    """Combine apply_async + delay call lists, filtered by leading positional arg."""
    publishes = []
    for c in apply_async_mock.call_args_list:
        args = c.kwargs.get("args") or c.args
        if args and args[0] == policy_path:
            publishes.append(("apply_async", c))
    for c in delay_mock.call_args_list:
        if c.args and c.args[0] == policy_path:
            publishes.append(("delay", c))
    return publishes


@override_settings(task_always_eager=True, WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
@freeze_time("2026-04-01")
class OfferingPolicyDebounceTest(test.APITestCase):
    """Regression: the offering trigger handler must debounce just like
    customer/project handlers (Sentry CSCS-4VC)."""

    def setUp(self):
        cache.clear()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = 2
        self.resource.save()
        self.invoice = invoices_models.Invoice.objects.get(
            customer=self.fixture.customer, month=4, year=2026
        )

    def test_rapid_invoice_item_saves_debounce_offering_handler(self):
        with (
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
            ) as apply_async,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.delay"
            ) as delay,
        ):
            cache.clear()  # setUp may have populated cache via auto-created items
            for _ in range(5):
                invoices_factories.InvoiceItemFactory(
                    invoice=self.invoice,
                    project=self.fixture.project,
                    resource=self.resource,
                    unit_price=Decimal("10"),
                    quantity=1,
                )

            publishes = _publishes_for(
                apply_async, delay, _OFFERING_ESTIMATED_COST_POLICY_PATH
            )
            self.assertEqual(
                len(publishes),
                1,
                f"Expected 1 debounced offering publish, got {len(publishes)}",
            )
            kind, call = publishes[0]
            self.assertEqual(kind, "apply_async")
            self.assertEqual(call.kwargs["countdown"], COST_POLICY_DEBOUNCE_SECONDS)

    def test_rapid_component_usage_saves_debounce_offering_usage_handler(self):
        with (
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
            ) as apply_async,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.delay"
            ) as delay,
        ):
            cache.clear()
            for _ in range(5):
                marketplace_factories.ComponentUsageFactory(resource=self.resource)

            publishes = _publishes_for(apply_async, delay, _OFFERING_USAGE_POLICY_PATH)
            self.assertEqual(len(publishes), 1)
            self.assertEqual(publishes[0][0], "apply_async")


@override_settings(task_always_eager=True, WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
@freeze_time("2026-04-01")
class CustomerComponentUsageDebounceTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = 2
        self.resource.save()
        self.component = self.fixture.offering_component

    def test_rapid_same_component_usage_saves_collapse_to_one_publish(self):
        with (
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
            ) as apply_async,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.delay"
            ) as delay,
        ):
            cache.clear()
            for _ in range(5):
                marketplace_factories.ComponentUsageFactory(
                    resource=self.resource, component=self.component
                )

            publishes = _publishes_for(
                apply_async, delay, _CUSTOMER_COMPONENT_USAGE_POLICY_PATH
            )
            self.assertEqual(len(publishes), 1)
            call = publishes[0][1]
            filters = call.kwargs["args"][1]
            self.assertIn("scope_id", filters)
            self.assertIn("component_limits_set__component_id", filters)

    def test_different_components_get_separate_publishes(self):
        c2 = marketplace_factories.OfferingComponentFactory(
            offering=self.fixture.offering, type="ram", name="RAM"
        )
        with (
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
            ) as apply_async,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.delay"
            ) as delay,
        ):
            cache.clear()
            marketplace_factories.ComponentUsageFactory(
                resource=self.resource, component=self.component
            )
            marketplace_factories.ComponentUsageFactory(
                resource=self.resource, component=c2
            )

            publishes = _publishes_for(
                apply_async, delay, _CUSTOMER_COMPONENT_USAGE_POLICY_PATH
            )
            self.assertEqual(
                len(publishes),
                2,
                "Different components on the same customer must not coalesce.",
            )


@override_settings(task_always_eager=True, WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
@freeze_time("2026-04-01")
class SlurmPeriodicUsageDebounceTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = 2
        self.resource.save()

    def test_no_publishes_without_policy(self):
        """The exists() gate must short-circuit before any publish."""
        with (
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_slurm_resource_policy.apply_async"
            ) as apply_async,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_slurm_resource_policy.delay"
            ) as delay,
        ):
            cache.clear()
            marketplace_factories.ComponentUsageFactory(resource=self.resource)

            self.assertEqual(apply_async.call_count, 0)
            self.assertEqual(delay.call_count, 0)

    def test_rapid_component_usage_saves_debounce_slurm_handler(self):
        policy_factories.SlurmPeriodicUsagePolicyFactory(scope=self.fixture.offering)
        with (
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_slurm_resource_policy.apply_async"
            ) as apply_async,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_slurm_resource_policy.delay"
            ) as delay,
        ):
            cache.clear()
            for _ in range(5):
                marketplace_factories.ComponentUsageFactory(resource=self.resource)

            self.assertEqual(apply_async.call_count, 1)
            self.assertEqual(delay.call_count, 0)
            call = apply_async.call_args
            self.assertEqual(call.kwargs["countdown"], COST_POLICY_DEBOUNCE_SECONDS)
            self.assertEqual(call.kwargs["args"], [str(self.resource.uuid)])


@override_settings(task_always_eager=True, WALDUR_COST_POLICY_DEBOUNCE_SECONDS=120)
@freeze_time("2026-04-01")
class CustomerCreditOfferingsListDebounceTest(test.APITestCase):
    """When offerings are linked/unlinked from a CustomerCredit, the
    customer policy evaluation must be debounced under the same cache key
    as ``customer_estimated_cost_policy_trigger_handler``."""

    def setUp(self):
        cache.clear()
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.resource.state = 2
        self.resource.save()
        self.invoice = invoices_models.Invoice.objects.get(
            customer=self.fixture.customer, month=4, year=2026
        )
        self.credit = invoices_factories.CustomerCreditFactory(
            customer=self.fixture.customer
        )

    def test_adding_offerings_debounces_to_one_publish(self):
        offerings = [marketplace_factories.OfferingFactory() for _ in range(5)]
        with (
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
            ) as apply_async,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.delay"
            ) as delay,
        ):
            cache.clear()
            self.credit.offerings.add(*offerings)

            publishes = _publishes_for(apply_async, delay, _CUSTOMER_POLICY_PATH)
            self.assertEqual(len(publishes), 1)
            self.assertEqual(publishes[0][0], "apply_async")

    def test_shares_debounce_key_with_invoice_item_handler(self):
        """A credit edit followed by an InvoiceItem save on the same customer
        must not double-publish — both routes share the per-customer cache key."""
        with (
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.apply_async"
            ) as apply_async,
            mock.patch(
                "waldur_mastermind.policy.tasks.evaluate_policies_async.delay"
            ) as delay,
        ):
            cache.clear()
            offering = marketplace_factories.OfferingFactory()
            self.credit.offerings.add(offering)
            invoices_factories.InvoiceItemFactory(
                invoice=self.invoice,
                project=self.fixture.project,
                resource=self.resource,
                unit_price=Decimal("10"),
                quantity=1,
            )

            publishes = _publishes_for(apply_async, delay, _CUSTOMER_POLICY_PATH)
            self.assertEqual(
                len(publishes),
                1,
                f"Credit-edit + invoice-item save must dedupe; got {publishes}",
            )
