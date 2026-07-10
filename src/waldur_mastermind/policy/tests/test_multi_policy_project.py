"""
Multi-resource / multi-policy cost-policy scenarios for a single project.

Models a realistic project holding several resources of different billing
types together with several ProjectEstimatedCostPolicies, and pins down when
pausing is triggered and when it is cleaned up.

Key semantics exercised (all verified against the implementation):

* A policy fires off the sum of ``InvoiceItem.total`` in its scope, compared
  to ``limit_cost`` (``policy/models.py`` ``_is_triggered``). Cost reaches an
  invoice item differently per billing type:
    - USAGE: only once usage is reported (needs a plan period) — a usage
      resource with no billed usage accrues nothing and cannot trip a policy;
    - LIMIT: accrues at provisioning, from the resource limit;
    - FIXED: accrues at registration.
  These tests create ``InvoiceItem`` rows directly (the established pattern) so
  ``total`` is a known value; the billing type is modelled by *where* the cost
  comes from.

* A project-wide policy (``resource=None``) pauses EVERY pausing-capable
  resource in the project; a resource-scoped policy pauses only its target.
  Only offerings with ``plugin_options.supports_pausing`` are ever paused.

* Cleanup (unpause) happens only on a ``has_fired`` True->False transition and
  is UNCONDITIONAL within the reset scope — there is no cross-policy
  coordination, so a resource-scoped reset can clobber a pause a still-firing
  project-wide policy wants (documented by the last test).
"""

from django.test import override_settings
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.policy import utils
from waldur_mastermind.policy.models import ProjectEstimatedCostPolicy
from waldur_mastermind.policy.tests import factories as policy_factories

TOTAL = ProjectEstimatedCostPolicy.Periods.TOTAL


@override_settings(task_always_eager=True, WALDUR_COST_POLICY_DEBOUNCE_SECONDS=0)
@freeze_time("2026-07-15")
class MultiPolicyProjectPausingTest(test.APITestCase):
    """One project, three resources (usage / limit / non-pausing), several
    cost policies. Cost is injected as invoice items; evaluation is driven
    explicitly so the fire/reset transitions are deterministic."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project

        # Usage-based, pausing-capable resource (cost arrives only when usage
        # is reported — here modelled by adding an invoice item on demand).
        self.usage_resource = self.fixture.resource
        self.usage_resource.state = ResourceStates.OK
        self.usage_resource.save()
        self._enable_pausing(self.usage_resource.offering)

        # Limit-based, pausing-capable resource on its own offering (in
        # production this accrues cost at provisioning).
        self.limit_offering = marketplace_factories.OfferingFactory(
            customer=self.customer
        )
        self._enable_pausing(self.limit_offering)
        self.limit_resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.limit_offering,
            state=ResourceStates.OK,
        )

        # A resource whose offering does NOT support pausing — must never be
        # paused, whatever the cost.
        self.nopause_offering = marketplace_factories.OfferingFactory(
            customer=self.customer
        )
        self.nopause_resource = marketplace_factories.ResourceFactory(
            project=self.project,
            offering=self.nopause_offering,
            state=ResourceStates.OK,
        )

        # Bringing the resources to OK state auto-creates the current-month
        # invoice, so get-or-create rather than collide on (customer, month,
        # year). tax_percent=0 keeps InvoiceItem.total == unit_price.
        self.invoice, _ = invoices_models.Invoice.objects.get_or_create(
            customer=self.customer, month=7, year=2026
        )
        self.invoice.tax_percent = 0
        self.invoice.save()
        # Start each scenario from a known-zero cost baseline: bringing the
        # resources to OK state auto-creates invoice items, which would
        # otherwise add uncontrolled cost on top of what each test injects.
        invoices_models.InvoiceItem.objects.filter(invoice=self.invoice).delete()

    # --- helpers -----------------------------------------------------------

    def _enable_pausing(self, offering):
        offering.plugin_options = {
            **(offering.plugin_options or {}),
            "supports_pausing": True,
        }
        offering.save()

    def _add_cost(self, resource, amount):
        """Add ``amount`` of cost attributed to ``resource`` (tax_percent=0 so
        InvoiceItem.total == unit_price)."""
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            resource=resource,
            unit_price=amount,
            quantity=1,
        )

    def _add_project_cost(self, amount):
        """Project cost not attributed to any resource (resource=None); used
        for credit-aware cases where per-resource compensation must stay out."""
        return invoices_factories.InvoiceItemFactory(
            invoice=self.invoice,
            project=self.project,
            unit_price=amount,
            quantity=1,
        )

    def _pausing_policy(self, limit_cost, resource=None, use_credit=False):
        return policy_factories.ProjectEstimatedCostPolicyFactory(
            scope=self.project,
            resource=resource,
            use_credit=use_credit,
            limit_cost=limit_cost,
            period=TOTAL,
            actions="request_pausing",
        )

    def _evaluate(self):
        """Run project policy evaluation exactly as the async task does."""
        utils.evaluate_policies(
            ProjectEstimatedCostPolicy.objects.filter(scope=self.project)
        )
        for r in (self.usage_resource, self.limit_resource, self.nopause_resource):
            r.refresh_from_db()

    # --- scenarios ---------------------------------------------------------

    def test_resource_scoped_policy_pauses_only_its_target(self):
        """A policy scoped to the limit resource pauses only it, even though
        the usage resource is also over the (same) threshold."""
        self._pausing_policy(limit_cost=5, resource=self.limit_resource)
        self._add_cost(self.limit_resource, 10)
        self._add_cost(self.usage_resource, 10)

        self._evaluate()

        self.assertTrue(self.limit_resource.paused)
        self.assertFalse(self.usage_resource.paused)
        self.assertFalse(self.nopause_resource.paused)

    def test_usage_resource_without_billed_usage_is_not_paused(self):
        """A usage resource with no reported usage accrues no cost, so its own
        resource-scoped policy never trips — while the limit resource, which
        does accrue, is paused by its policy."""
        self._pausing_policy(limit_cost=1, resource=self.usage_resource)
        self._pausing_policy(limit_cost=1, resource=self.limit_resource)
        self._add_cost(self.limit_resource, 10)  # usage_resource gets nothing

        self._evaluate()

        self.assertFalse(self.usage_resource.paused)
        self.assertTrue(self.limit_resource.paused)

    def test_project_wide_policy_pauses_all_pausing_capable_resources(self):
        """A project-wide policy pauses every pausing-capable resource; the
        non-pausing offering's resource is left alone."""
        self._pausing_policy(limit_cost=5)  # project-wide
        self._add_cost(self.usage_resource, 10)

        self._evaluate()

        self.assertTrue(self.usage_resource.paused)
        self.assertTrue(self.limit_resource.paused)
        self.assertFalse(self.nopause_resource.paused)

    def test_project_total_aggregates_costs_across_resources(self):
        """Each resource alone is under the project limit, but their combined
        cost crosses it and pauses the project."""
        self._pausing_policy(limit_cost=15)  # project-wide
        self._add_cost(self.usage_resource, 10)
        self._add_cost(self.limit_resource, 10)  # combined 20 > 15

        self._evaluate()

        self.assertTrue(self.usage_resource.paused)
        self.assertTrue(self.limit_resource.paused)

    def test_below_limit_does_not_pause(self):
        self._pausing_policy(limit_cost=100, resource=self.limit_resource)
        self._add_cost(self.limit_resource, 10)

        self._evaluate()

        policy = ProjectEstimatedCostPolicy.objects.get(scope=self.project)
        self.assertFalse(policy.has_fired)
        self.assertFalse(self.limit_resource.paused)

    def test_cleanup_unpauses_when_cost_drops_below_limit(self):
        """A True->False transition unpauses the resource and clears has_fired."""
        policy = self._pausing_policy(limit_cost=5, resource=self.limit_resource)
        item = self._add_cost(self.limit_resource, 10)

        self._evaluate()
        policy.refresh_from_db()
        self.assertTrue(policy.has_fired)
        self.assertTrue(self.limit_resource.paused)

        # Cost drops (deleting an item does not itself re-trigger evaluation).
        item.delete()
        self._evaluate()

        policy.refresh_from_db()
        self.assertFalse(policy.has_fired)
        self.assertFalse(self.limit_resource.paused)

    def test_credit_aware_policy_defers_pausing_until_credit_is_low(self):
        """A use_credit policy holds off while customer credit exceeds the
        limit, and pauses once credit drops to the limit. Cost is attributed to
        no resource so per-resource compensation stays out of the picture."""
        credit = invoices_factories.CustomerCreditFactory(
            customer=self.customer, value=100_000
        )
        self._pausing_policy(limit_cost=100, use_credit=True)  # project-wide
        self._add_project_cost(500)  # cost > limit, but credit is ample

        self._evaluate()
        self.assertFalse(self.usage_resource.paused)
        self.assertFalse(self.limit_resource.paused)

        # Credit runs down to the limit -> policy now fires.
        credit.value = 100
        credit.save()
        self._evaluate()

        self.assertTrue(self.usage_resource.paused)
        self.assertTrue(self.limit_resource.paused)

    def test_resource_scoped_reset_respects_project_wide_pause(self):
        """A resource-scoped policy's reset must not unpause a resource a
        still-firing project-wide policy wants paused.

        Both a project-wide policy and a policy scoped to the usage resource
        fire, pausing the usage resource. When only the usage resource's own
        cost drops (project total still over the limit), the resource-scoped
        policy stops firing and resets — but the resource stays paused because
        the project-wide policy still wants it so. (Without the cross-policy
        guard this was a last-writer-wins clobber that left it wrongly
        unpaused.)"""
        self._pausing_policy(limit_cost=5)  # project-wide (P_project)
        self._pausing_policy(
            limit_cost=5, resource=self.usage_resource
        )  # resource-scoped (P_res)

        usage_item = self._add_cost(self.usage_resource, 10)
        self._add_cost(self.limit_resource, 10)  # keeps project total high

        self._evaluate()
        self.assertTrue(self.usage_resource.paused)
        self.assertTrue(self.limit_resource.paused)

        # Usage resource's own cost drops below its policy limit, but the
        # project total (from the limit resource) is still over the limit.
        usage_item.delete()
        self._evaluate()

        p_project = ProjectEstimatedCostPolicy.objects.get(
            scope=self.project, resource__isnull=True
        )
        p_res = ProjectEstimatedCostPolicy.objects.get(
            scope=self.project, resource=self.usage_resource
        )
        self.assertTrue(p_project.has_fired)  # project policy still firing
        self.assertFalse(p_res.has_fired)  # resource policy reset
        # Stays paused: the project-wide policy still wants it paused.
        self.assertTrue(self.usage_resource.paused)
        self.assertTrue(self.limit_resource.paused)

    def test_reset_unpauses_once_all_policies_stop_firing(self):
        """When the last policy wanting a resource paused stops firing, the
        resource is finally unpaused (the guard only defers, never blocks)."""
        self._pausing_policy(limit_cost=5)  # project-wide
        self._pausing_policy(limit_cost=5, resource=self.usage_resource)

        usage_item = self._add_cost(self.usage_resource, 10)
        limit_item = self._add_cost(self.limit_resource, 10)

        self._evaluate()
        self.assertTrue(self.usage_resource.paused)

        # All cost drops -> both policies stop firing -> resource unpaused.
        usage_item.delete()
        limit_item.delete()
        self._evaluate()

        self.assertFalse(self.usage_resource.paused)
        self.assertFalse(self.limit_resource.paused)
