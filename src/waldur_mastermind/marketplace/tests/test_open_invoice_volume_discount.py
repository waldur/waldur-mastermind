"""Estimated volume discounts on open invoices.

Month close is not the only pass any more: an open invoice carries the same
discount lines, marked as estimates, so its total, the credit forecast and the
cost policies show what the month will be charged. These tests cover the
passes over an open invoice and what reads the lines they write.
"""

import uuid
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db.models import signals
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.invoices import handlers as invoices_handlers
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import tasks as invoices_tasks
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import billing_discount, handlers, tasks
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import BillingTypes, ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.policy.tests import factories as policy_factories


@freeze_time("2024-01-15")
class OpenInvoiceDiscountTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.offering = marketplace_factories.OfferingFactory(customer=self.customer)
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            name="RAM",
            billing_type=BillingTypes.USAGE,
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
            price=Decimal("10"),
            discount_formula="50 if usage >= 10 else 0",
        )
        self.invoice = invoice_factories.InvoiceFactory(customer=self.customer)

    def _add_item(self, usage, invoice=None, project=None):
        invoice = invoice or self.invoice
        project = project or self.project
        resource = marketplace_factories.ResourceFactory(
            offering=self.offering, plan=self.plan, project=project
        )
        now = timezone.now()
        return invoices_models.InvoiceItem.objects.create(
            invoice=invoice,
            resource=resource,
            plan_component=self.plan_component,
            project=project,
            unit_price=self.plan_component.price,
            quantity=Decimal(usage),
            unit=invoices_models.Units.QUANTITY,
            start=now,
            end=now,
            details={
                "offering_component_type": self.offering_component.type,
                billing_discount.DISCOUNT_USAGE_KEY: float(usage),
            },
        )

    def _set_usage(self, item, usage):
        item.quantity = Decimal(usage)
        item.details[billing_discount.DISCOUNT_USAGE_KEY] = float(usage)
        item.save(update_fields=["quantity", "details"])

    def _lines(self, invoice=None):
        return (invoice or self.invoice).items.filter(details__is_discount=True)


class EstimatedLinesTest(OpenInvoiceDiscountTest):
    def test_lines_of_an_open_invoice_are_marked_as_estimates(self):
        self._add_item(usage=10)

        billing_discount.refresh_estimated_discounts(self.invoice)

        line = self._lines().get()
        self.assertTrue(line.details[billing_discount.ESTIMATE_KEY])
        self.assertTrue(line.name.endswith("Volume discount (50%, estimated)"))
        self.assertEqual(line.unit_price, Decimal("-50"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.price, Decimal("50"))

    def test_month_close_turns_the_estimate_into_a_final_line_in_place(self):
        self._add_item(usage=10)
        billing_discount.refresh_estimated_discounts(self.invoice)
        estimate = self._lines().get()

        billing_discount.apply_aggregated_volume_discounts(self.invoice)

        line = self._lines().get()
        self.assertEqual(line.uuid, estimate.uuid)
        self.assertNotIn(billing_discount.ESTIMATE_KEY, line.details)
        self.assertTrue(line.name.endswith("Volume discount (50%)"))
        self.assertEqual(line.unit_price, Decimal("-50"))

    def test_a_pass_with_nothing_changed_writes_nothing(self):
        self._add_item(usage=10)
        self._add_item(usage=4)
        billing_discount.refresh_estimated_discounts(self.invoice)

        writes = []

        def record(sender, instance, **kwargs):
            writes.append(instance)

        signals.post_save.connect(record, sender=invoices_models.InvoiceItem)
        signals.post_delete.connect(record, sender=invoices_models.InvoiceItem)
        try:
            billing_discount.refresh_estimated_discounts(self.invoice)
        finally:
            signals.post_save.disconnect(record, sender=invoices_models.InvoiceItem)
            signals.post_delete.disconnect(record, sender=invoices_models.InvoiceItem)

        self.assertEqual(writes, [])
        self.assertEqual(self._lines().count(), 2)

    def test_changed_usage_updates_the_line_in_place(self):
        item = self._add_item(usage=10)
        billing_discount.refresh_estimated_discounts(self.invoice)
        before = self._lines().get()

        self._set_usage(item, 20)
        billing_discount.refresh_estimated_discounts(self.invoice)

        line = self._lines().get()
        self.assertEqual(line.uuid, before.uuid)
        self.assertEqual(line.unit_price, Decimal("-100"))
        self.assertEqual(line.details["aggregated_usage"], 20.0)

    def test_a_line_that_no_longer_applies_is_deleted_and_estimates_follow(self):
        item = self._add_item(usage=10)
        billing_discount.refresh_estimated_discounts(self.invoice)

        # Below the tier: the line must go, and with it its share of the
        # project's estimate, which no delete handler would otherwise refresh.
        self._set_usage(item, 5)
        billing_discount.refresh_estimated_discounts(self.invoice)

        self.assertFalse(self._lines().exists())
        estimate = billing_models.PriceEstimate.objects.get(scope=self.project)
        self.assertEqual(estimate.total, Decimal("50"))

    def test_a_second_line_for_one_item_is_removed(self):
        self._add_item(usage=10)
        billing_discount.refresh_estimated_discounts(self.invoice)
        line = self._lines().get()
        duplicate = invoices_models.InvoiceItem.objects.get(pk=line.pk)
        duplicate.pk = None
        duplicate.uuid = uuid.uuid4().hex
        duplicate.save()
        self.assertEqual(self._lines().count(), 2)

        billing_discount.refresh_estimated_discounts(self.invoice)

        self.assertEqual(self._lines().count(), 1)

    def test_an_estimate_pass_leaves_a_closed_invoice_alone(self):
        self._add_item(usage=10)
        self.invoice.state = invoices_models.Invoice.States.CREATED
        self.invoice.save(update_fields=["state"])

        billing_discount.refresh_estimated_discounts(self.invoice)

        self.assertFalse(self._lines().exists())


class DailyRefreshTest(OpenInvoiceDiscountTest):
    def test_open_invoices_get_estimates_and_their_total_follows(self):
        self._add_item(usage=10)

        invoices_tasks.update_invoices_total_cost()

        self.assertTrue(self._lines().get().details[billing_discount.ESTIMATE_KEY])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total_price, Decimal("50"))

    def test_a_closed_invoice_of_the_month_is_not_touched(self):
        other_customer = structure_factories.CustomerFactory()
        closed = invoice_factories.InvoiceFactory(
            customer=other_customer, state=invoices_models.Invoice.States.CREATED
        )
        self._add_item(
            usage=10,
            invoice=closed,
            project=structure_factories.ProjectFactory(customer=other_customer),
        )

        invoices_tasks.update_invoices_total_cost()

        self.assertFalse(self._lines(closed).exists())

    def test_invoices_without_discounts_are_not_loaded(self):
        self.plan_component.discount_formula = ""
        self.plan_component.save()
        self._add_item(usage=10)

        with mock.patch.object(
            billing_discount, "refresh_estimated_discounts"
        ) as refresh:
            invoices_tasks.update_invoices_total_cost()

        refresh.assert_not_called()


class QueuedRefreshTest(OpenInvoiceDiscountTest):
    def test_changes_in_a_burst_queue_one_refresh(self):
        with mock.patch.object(tasks.refresh_volume_discounts, "apply_async") as queued:
            handlers.queue_volume_discount_refresh(self.customer.id)
            handlers.queue_volume_discount_refresh(self.customer.id)

        queued.assert_called_once_with(
            args=(self.customer.id,),
            countdown=billing_discount.REFRESH_DEBOUNCE_SECONDS,
        )

    def test_a_rolled_back_change_does_not_hold_the_debounce(self):
        # The commit callback is dropped, as when the transaction rolls back:
        # nothing may be claimed before it, so the next real change queues.
        with (
            mock.patch.object(handlers.transaction, "on_commit") as on_commit,
            mock.patch.object(tasks.refresh_volume_discounts, "apply_async") as queued,
        ):
            handlers.queue_volume_discount_refresh(self.customer.id)

        on_commit.assert_called_once()
        queued.assert_not_called()
        self.assertIsNone(
            cache.get(billing_discount.refresh_debounce_key(self.customer.id))
        )

    def test_a_failed_publish_releases_the_debounce(self):
        with mock.patch.object(
            tasks.refresh_volume_discounts,
            "apply_async",
            side_effect=ConnectionError("broker down"),
        ):
            handlers.queue_volume_discount_refresh(self.customer.id)

        self.assertIsNone(
            cache.get(billing_discount.refresh_debounce_key(self.customer.id))
        )

    def test_a_cache_outage_after_commit_is_contained(self):
        # The change has committed by now: the callback must not raise.
        with (
            mock.patch.object(
                handlers.cache, "add", side_effect=ConnectionError("cache down")
            ),
            mock.patch.object(tasks.refresh_volume_discounts, "apply_async") as queued,
        ):
            handlers.queue_volume_discount_refresh(self.customer.id)

        queued.assert_not_called()

    def test_nothing_is_queued_where_no_plan_has_a_formula(self):
        self.plan_component.discount_formula = ""
        self.plan_component.save()

        with mock.patch.object(tasks.refresh_volume_discounts, "apply_async") as queued:
            handlers.queue_volume_discount_refresh(self.customer.id)

        queued.assert_not_called()

    def test_termination_queues_a_refresh(self):
        resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            plan=self.plan,
            project=self.project,
            state=ResourceStates.TERMINATING,
        )

        with mock.patch.object(tasks.refresh_volume_discounts, "apply_async") as queued:
            resource.set_state_terminated()
            resource.save()

        queued.assert_called_once()

    def test_the_task_refreshes_open_invoices_and_releases_the_debounce(self):
        self._add_item(usage=10)
        key = billing_discount.refresh_debounce_key(self.customer.id)
        cache.add(key, True)

        tasks.refresh_volume_discounts(self.customer.id)

        self.assertTrue(self._lines().get().details[billing_discount.ESTIMATE_KEY])
        self.assertIsNone(cache.get(key))


class MoveTest(OpenInvoiceDiscountTest):
    """A move takes estimated lines to another customer's invoice and changes
    both customers' volumes, so both open invoices are rebuilt."""

    def setUp(self):
        super().setUp()
        self.other_customer = structure_factories.CustomerFactory()
        self.other_project = structure_factories.ProjectFactory(
            customer=self.other_customer
        )
        self.other_invoice = invoice_factories.InvoiceFactory(
            customer=self.other_customer
        )
        # 6 + 6 crosses the 10 tier: 50% on both, while they share an invoice.
        self.staying = self._add_item(usage=6)
        self.moving = self._add_item(
            usage=6, project=structure_factories.ProjectFactory(customer=self.customer)
        )
        billing_discount.refresh_estimated_discounts(self.invoice)
        self.assertEqual(self._lines().count(), 2)

    def test_moving_a_resource_rebuilds_both_invoices(self):
        marketplace_utils.move_resource(self.moving.resource, self.other_project)

        # Neither customer reaches the tier on its own any more.
        self.assertFalse(self._lines().exists())
        self.assertFalse(self._lines(self.other_invoice).exists())

    def test_moving_a_project_rebuilds_both_invoices(self):
        invoices_handlers.projects_customer_has_been_changed(
            sender=None,
            project=self.moving.project,
            old_customer=self.customer,
            new_customer=self.other_customer,
        )

        self.assertTrue(self.other_invoice.items.filter(pk=self.moving.pk).exists())
        self.assertFalse(self._lines().exists())
        self.assertFalse(self._lines(self.other_invoice).exists())


class ReadersOfTheEstimateTest(OpenInvoiceDiscountTest):
    """What reads the open invoice already nets out discount lines; once they
    exist it reports what the month will be charged."""

    def test_credit_forecast_is_net_of_the_estimated_discount(self):
        invoice_factories.CustomerCreditFactory(
            customer=self.customer, value=Decimal("1000")
        )
        project_credit = invoice_factories.ProjectCreditFactory(
            project=self.project, value=Decimal("1000")
        )
        self._add_item(usage=10)
        self.assertEqual(project_credit.creditable_cost_this_month, Decimal("100"))

        billing_discount.refresh_estimated_discounts(self.invoice)

        self.assertEqual(project_credit.creditable_cost_this_month, Decimal("50"))

    def test_cost_policy_is_net_of_the_estimated_discount(self):
        policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project, limit_cost=80, use_credit=False
        )
        self._add_item(usage=10)
        self.assertTrue(policy.is_triggered())

        billing_discount.refresh_estimated_discounts(self.invoice)

        self.assertFalse(policy.is_triggered())

    def test_invoice_reports_its_volume_discounts(self):
        self._add_item(usage=10)
        billing_discount.refresh_estimated_discounts(self.invoice)

        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.get(
            invoice_factories.InvoiceFactory.get_url(self.invoice)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["volume_discounts"]), Decimal("-50"))
        self.assertEqual(Decimal(response.data["price"]), Decimal("50"))
