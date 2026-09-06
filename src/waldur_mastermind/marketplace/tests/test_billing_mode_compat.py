"""Does migration 0284 change how anything is billed?

The migration moves plans off ``inherit``, where the stored component values
apply, onto an explicit mode, where they are recomputed. The two must agree
for every offering that exists today, or the migration silently reprices live
subscriptions. Each scenario below snapshots the effective components before
the stamp and compares them after.
"""

import importlib

from django.apps import apps
from django.test import TestCase

from waldur_mastermind.marketplace import billing_mode, models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    RANCHER_OFFERING,
    VMWARE_VM_OFFERING,
    BillingModes,
    BillingTypes,
    LimitPeriods,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.utils import create_offering_components

MIGRATION = importlib.import_module(
    "waldur_mastermind.marketplace.migrations.0284_stamp_plan_billing_mode"
)


def snapshot(plan):
    """Effective billing of every component under this plan."""
    return {
        effective.type: (
            effective.billing_type,
            effective.is_prepaid,
            effective.limit_period,
            effective.measured_unit,
        )
        for effective in billing_mode.resolve_plan(plan).components.values()
    }


def build(offering_type, mutate=None):
    offering = factories.OfferingFactory(type=offering_type)
    if offering_type == OPENSTACK_TENANT_OFFERING:
        create_offering_components(offering)
    else:
        from waldur_mastermind.marketplace.plugins import manager

        for component in manager.get_components(offering_type):
            factories.OfferingComponentFactory(
                offering=offering, billed_per_plan=True, **component._asdict()
            )
    if mutate:
        mutate(offering)
    plan = factories.PlanFactory(offering=offering)
    return offering, plan


class MigrationIsBehaviourPreservingTest(TestCase):
    def assert_stamp_preserves_billing(self, offering, plan, expected_mode):
        before = snapshot(plan)
        MIGRATION.stamp_billing_mode(apps, None)
        plan.refresh_from_db()
        after = snapshot(plan)
        self.assertEqual(plan.billing_mode, expected_mode)
        self.assertEqual(
            before,
            after,
            f"\n{offering.type} stamped {plan.billing_mode!r} changed billing:\n"
            + "\n".join(
                f"  {k}: {before.get(k)} -> {after.get(k)}"
                for k in sorted(set(before) | set(after))
                if before.get(k) != after.get(k)
            ),
        )

    # --- scenarios as shipped -------------------------------------------

    def test_openstack_default(self):
        offering, plan = build(OPENSTACK_TENANT_OFFERING)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.LIMIT)

    def test_openstack_switched_to_usage(self):
        def mutate(offering):
            offering.components.update(billing_type=BillingTypes.USAGE)
            for c in offering.components.all():
                c.measured_unit = "core-hours" if c.type == "cores" else "GB-hours"
                c.save(update_fields=["measured_unit"])

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.USAGE)

    def test_openstack_switched_to_prepaid_keeps_inherit(self):
        # Prepaid is not a plan mode; the components go on governing it.
        def mutate(offering):
            offering.components.update(
                billing_type=BillingTypes.ONE_TIME, is_prepaid=True
            )

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    def test_openstack_with_volume_type(self):
        def mutate(offering):
            factories.OfferingComponentFactory(
                offering=offering,
                type="gigabytes_ssd",
                name="Storage (ssd)",
                measured_unit="GB",
                billing_type=BillingTypes.LIMIT,
                limit_period=LimitPeriods.MONTH,
                billed_per_plan=True,
            )

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.LIMIT)

    # The stamp is confined to OpenStack, so no other plugin's plans move off
    # inherit and nothing about those deployments changes.

    def test_vmware_is_not_stamped(self):
        offering, plan = build(VMWARE_VM_OFFERING)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    def test_rancher_is_not_stamped(self):
        offering, plan = build(RANCHER_OFFERING)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    def test_openportal_is_not_stamped(self):
        offering, plan = build("Marketplace.OpenPortal")
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    # --- scenarios a provider can produce through the API ----------------
    #
    # An explicit mode recomputes the limit period and the measured unit, so it
    # cannot reproduce a component the provider has tuned. These keep inherit;
    # stamping them would reprice live subscriptions -- a total limit period is
    # billed once on creation and a monthly one every month, and an annual one
    # would be charged twelve times a year instead of once.

    def test_openstack_with_a_customised_measured_unit(self):
        def mutate(offering):
            offering.components.filter(type="cores").update(measured_unit="vCPU")

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    def test_openstack_with_an_annual_limit_period(self):
        def mutate(offering):
            offering.components.update(limit_period=LimitPeriods.ANNUAL)

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    def test_openstack_with_a_total_limit_period(self):
        def mutate(offering):
            offering.components.update(limit_period=LimitPeriods.TOTAL)

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    def test_openstack_usage_with_a_customised_unit(self):
        def mutate(offering):
            offering.components.update(billing_type=BillingTypes.USAGE)
            offering.components.filter(type="cores").update(measured_unit="CPU-hours")

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    # --- scenarios that must stay on inherit -----------------------------

    def test_mixed_components_stay_on_inherit(self):
        def mutate(offering):
            offering.components.filter(type="cores").update(
                billing_type=BillingTypes.USAGE
            )

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        self.assert_stamp_preserves_billing(offering, plan, BillingModes.INHERIT)

    def test_custom_component_is_untouched(self):
        def mutate(offering):
            factories.OfferingComponentFactory(
                offering=offering,
                type="consultancy",
                name="Consultancy",
                measured_unit="hours",
                billing_type=BillingTypes.USAGE,
            )

        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        before = snapshot(plan)
        MIGRATION.stamp_billing_mode(apps, None)
        plan.refresh_from_db()
        after = snapshot(plan)
        self.assertEqual(before["consultancy"], after["consultancy"])
        self.assertEqual(before, after)

    def test_resources_on_a_stamped_plan_keep_their_flags(self):
        offering, plan = build(OPENSTACK_TENANT_OFFERING)
        resource = factories.ResourceFactory(offering=offering, plan=plan)
        before = (resource.is_usage_based, resource.is_limit_based)
        MIGRATION.stamp_billing_mode(apps, None)
        resource.refresh_from_db()
        self.assertEqual(before, (resource.is_usage_based, resource.is_limit_based))
        self.assertTrue(
            models.Resource.objects.filter(pk=resource.pk)
            .filter(billing_mode.limit_resource_q())
            .exists()
        )


class InvoiceIsUnchangedByTheStampTest(TestCase):
    """The same resource must produce the same invoice lines either side.

    The resolved-component comparison above is the mechanism; this is the
    outcome it exists to protect.
    """

    def bill(self, offering, plan, limits):
        from waldur_mastermind.marketplace.billing import MarketplaceBillingService

        resource = factories.ResourceFactory(
            offering=offering,
            plan=plan,
            limits=limits,
            state=models.Resource.States.OK,
        )
        MarketplaceBillingService.handle_resource_creation(resource)
        from waldur_mastermind.invoices import models as invoice_models

        items = invoice_models.InvoiceItem.objects.filter(resource=resource)
        return sorted(
            (
                # The resource name is part of item.name and differs between
                # the two runs; the component after the last slash is what
                # identifies the line.
                item.name.rsplit(" / ", 1)[-1],
                item.unit,
                str(item.unit_price),
                str(item.quantity),
                str(item.total),
                (item.details or {}).get("limit_period"),
                (item.details or {}).get("offering_component_type"),
            )
            for item in items
        )

    def scenario(self, mutate=None):
        offering, plan = build(OPENSTACK_TENANT_OFFERING, mutate)
        for component in offering.components.all():
            factories.PlanComponentFactory(
                plan=plan, component=component, price=10, amount=1
            )
        return offering, plan

    def assert_invoice_is_unchanged(self, mutate=None):
        limits = {"cores": 2, "ram": 2048, "storage": 10240}
        offering, plan = self.scenario(mutate)
        before = self.bill(offering, plan, limits)
        MIGRATION.stamp_billing_mode(apps, None)
        plan.refresh_from_db()
        after = self.bill(offering, plan, limits)
        self.assertEqual(
            before,
            after,
            f"\nplan is now {plan.billing_mode!r} and the invoice changed:"
            f"\n  before: {before}\n  after:  {after}",
        )
        return plan

    def test_default_openstack_offering(self):
        plan = self.assert_invoice_is_unchanged()
        self.assertEqual(plan.billing_mode, BillingModes.LIMIT)

    def test_annual_limit_period(self):
        def mutate(offering):
            offering.components.update(limit_period=LimitPeriods.ANNUAL)

        plan = self.assert_invoice_is_unchanged(mutate)
        self.assertEqual(plan.billing_mode, BillingModes.INHERIT)

    def test_total_limit_period(self):
        def mutate(offering):
            offering.components.update(limit_period=LimitPeriods.TOTAL)

        plan = self.assert_invoice_is_unchanged(mutate)
        self.assertEqual(plan.billing_mode, BillingModes.INHERIT)

    def test_prepaid_offering(self):
        def mutate(offering):
            offering.components.update(
                billing_type=BillingTypes.ONE_TIME, is_prepaid=True
            )

        plan = self.assert_invoice_is_unchanged(mutate)
        self.assertEqual(plan.billing_mode, BillingModes.INHERIT)

    def test_usage_offering(self):
        # The other shape that exists in the wild, and the one the stamp does
        # move off inherit: switch_billing_mode rewrites the type and the units
        # together, so the mode reproduces it exactly.
        def mutate(offering):
            offering.components.update(billing_type=BillingTypes.USAGE)
            for component in offering.components.all():
                component.measured_unit = (
                    "core-hours" if component.type == "cores" else "GB-hours"
                )
                component.save(update_fields=["measured_unit"])

        plan = self.assert_invoice_is_unchanged(mutate)
        self.assertEqual(plan.billing_mode, BillingModes.USAGE)

    def test_quarterly_limit_period(self):
        def mutate(offering):
            offering.components.update(limit_period=LimitPeriods.QUARTERLY)

        plan = self.assert_invoice_is_unchanged(mutate)
        self.assertEqual(plan.billing_mode, BillingModes.INHERIT)

    def test_one_time_components_that_are_not_prepaid(self):
        # Charged once at creation without being a prepaid subscription: no plan
        # mode reproduces it, so the components go on governing.
        def mutate(offering):
            offering.components.update(
                billing_type=BillingTypes.ONE_TIME, is_prepaid=False
            )

        plan = self.assert_invoice_is_unchanged(mutate)
        self.assertEqual(plan.billing_mode, BillingModes.INHERIT)

    def test_fixed_components(self):
        def mutate(offering):
            offering.components.update(billing_type=BillingTypes.FIXED)

        plan = self.assert_invoice_is_unchanged(mutate)
        self.assertEqual(plan.billing_mode, BillingModes.INHERIT)

    def test_one_component_tuned_and_the_rest_monthly(self):
        # The components agree on billing type but not on period, so the mode
        # would normalise the odd one out.
        def mutate(offering):
            offering.components.filter(type="storage").update(
                limit_period=LimitPeriods.TOTAL
            )

        plan = self.assert_invoice_is_unchanged(mutate)
        self.assertEqual(plan.billing_mode, BillingModes.INHERIT)
