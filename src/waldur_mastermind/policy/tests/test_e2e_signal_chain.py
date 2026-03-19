"""End-to-end test validating the full signal chain:

InvoiceItem.save()
  -> post_save signal
  -> handler schedules Celery task (evaluate_policies_async)
  -> task calls evaluate_policies()
  -> policy fires request_pausing()
  -> resource.save(update_fields=["paused"])
  -> post_save signal fires on Resource

This validates that the Celery delegation + .save() fix work together
to produce the post_save signals needed for STOMP notifications.
"""

from unittest import mock

from django.db.models.signals import post_save
from django.test import override_settings
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy.tests import factories as policy_factories


@override_settings(task_always_eager=True)
@freeze_time("2024-09-01")
class EndToEndPolicySignalChainTest(test.APITestCase):
    """Validate the full chain: InvoiceItem save -> Celery -> policy -> resource.save() -> post_save."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.save()

        self.resource.offering.plugin_options = {"supports_pausing": True}
        self.resource.offering.save()

        self.policy = policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            created_by=self.fixture.user,
            limit_cost=50,
            actions="request_pausing",
        )

        self.invoice, _ = invoices_models.Invoice.objects.get_or_create(
            customer=self.customer,
            month=9,
            year=2024,
            defaults={"tax_percent": 0},
        )

    def _connect_signal(self):
        handler = mock.MagicMock()
        post_save.connect(handler, sender=marketplace_models.Resource)
        self.addCleanup(
            post_save.disconnect, handler, sender=marketplace_models.Resource
        )
        return handler

    def test_invoice_item_triggers_full_chain_and_fires_post_save(self):
        """Creating an invoice item above the limit should pause the resource
        and fire post_save on Resource - all via Celery task."""
        self.assertFalse(self.resource.paused)
        self.assertFalse(self.policy.has_fired)

        handler = self._connect_signal()

        # This triggers: InvoiceItem.save() -> signal -> Celery task ->
        # evaluate_policies -> request_pausing -> resource.save() -> post_save
        invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            quantity=1,
            unit_price=100,  # exceeds limit_cost=50
        )

        self.resource.refresh_from_db()
        self.policy.refresh_from_db()

        self.assertTrue(self.resource.paused)
        self.assertTrue(self.policy.has_fired)
        handler.assert_called()

        # Verify the signal included the paused=True state
        pausing_calls = [
            c
            for c in handler.call_args_list
            if c[1].get("instance")
            and c[1]["instance"].pk == self.resource.pk
            and c[1]["instance"].paused
            and c[1].get("update_fields")
        ]
        self.assertTrue(
            pausing_calls,
            "post_save signal with paused=True and update_fields not found",
        )

    def test_reset_path_fires_post_save_with_unpaused(self):
        """Lowering cost below the limit should unpause the resource
        and fire post_save on Resource."""
        # First trigger the policy
        item = invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            quantity=1,
            unit_price=100,
        )

        self.resource.refresh_from_db()
        self.assertTrue(self.resource.paused)

        handler = self._connect_signal()

        # Now lower cost below limit to trigger reset
        item.unit_price = 10
        item.save()

        self.resource.refresh_from_db()
        self.policy.refresh_from_db()

        self.assertFalse(self.resource.paused)
        self.assertFalse(self.policy.has_fired)
        handler.assert_called()

        # Verify the signal included the paused=False state
        unpausing_calls = [
            c
            for c in handler.call_args_list
            if c[1].get("instance")
            and c[1]["instance"].pk == self.resource.pk
            and not c[1]["instance"].paused
            and c[1].get("update_fields")
        ]
        self.assertTrue(
            unpausing_calls,
            "post_save signal with paused=False and update_fields not found",
        )
