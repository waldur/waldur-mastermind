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
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.handlers import (
    COST_POLICY_DEBOUNCE_SECONDS,
    _debounced_evaluate,
)


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

    def test_rapid_saves_debounce_to_two_tasks(self):
        """5 rapid InvoiceItem saves should result in at most 2 scheduled tasks
        (one per unique debounce key: project + customer), even though
        _debounced_evaluate is called many times."""
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

            # Count only debounced calls (those with countdown kwarg).
            # Other handlers (.delay()) also hit apply_async but without countdown.
            debounced_calls = [
                c
                for c in mock_apply.call_args_list
                if c.kwargs.get("countdown") == COST_POLICY_DEBOUNCE_SECONDS
            ]
            self.assertEqual(
                len(debounced_calls),
                2,
                f"Expected 2 debounced tasks (project + customer), "
                f"got {len(debounced_calls)}.",
            )
