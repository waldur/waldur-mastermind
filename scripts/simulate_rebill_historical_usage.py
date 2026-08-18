"""Simulate the "historical usage backfill leaves the invoice stale" incident
that `rebill_historical_usage` is meant to fix, so it can be exercised
manually against a local Waldur instance -- now covering all three directions
of a correction and their effect on Cost Policies, not just the invoice/credit
math in isolation.

Intended to be run inside `waldur shell` (Django's interactive shell), e.g.:

    uv run waldur shell < scripts/simulate_rebill_historical_usage.py

or by pasting its contents into an interactive `uv run waldur shell` session.

Builds a self-contained customer with THREE projects, each with its own
resource/usage/credit/ProjectEstimatedCostPolicy, covering the three Cost
Policy outcomes a correction can produce:

  Scenario A (project "Rebill Historical Usage Test Project A", resource
  "cluster-a-01"): usage was wrongly INFLATED, billed and compensated while
  the invoice was still mutable -- exactly what `waldur_site_load_historical_usage`
  is correcting when it runs against an already-finalized month. Its Cost
  Policy has already fired (`has_fired=True`) and the resource is marked
  paused, mirroring what a real policy evaluation would have done against the
  stale numbers. Correcting the usage down is expected to flip the policy's
  gates back closed -- `rebill_historical_usage`'s new policy-impact preview
  should report `fired: True -> False` and list the resource as reset.

  Scenario B (project "Rebill Historical Usage Test Project B", resource
  "cluster-b-01"): usage was UNDER-reported and gets corrected upward. The
  credit backing it is deliberately too small to cover the increase, so the
  credit correction is expected to ABORT (balance left untouched, flagged for
  manual review) while the cost correction still lands on the invoice. That
  higher, uncompensated cost is expected to newly open both of the policy's
  gates -- the preview should report `fired: False -> True` and list the
  resource as newly paused.

  Scenario C (project "Rebill Historical Usage Test Project C", resource
  "cluster-c-01"): usage was UNDER-reported and gets corrected upward, same
  direction as B -- but this project's credit comfortably covers the
  increase, so the credit correction succeeds instead of aborting. The
  corrected cost is fully compensated again (net cost stays near zero), so
  the policy's gates stay closed throughout -- the preview should report
  `fired: False -> False` (no transition) with the credit reduced by exactly
  the delta and no ERROR line. This is the "boring", most common case: a
  clean correction that never gets near either gate.

All three resources are billed onto the SAME customer invoice for the same
month, so the one real `MonthlyCompensation.apply_compensations()` call below
processes them together, cheapest-first, exactly as production billing does
-- this is deliberate, not an oversight; it's what makes the three projects'
starting credit balances interact realistically instead of being computed in
isolation.

The billing month is always "last calendar month" relative to whenever this
script runs (not a fixed date) -- a Cost Policy's rolling window is anchored
to *today*, so a hardcoded past month would eventually fall outside every
window and the scenario would silently stop demonstrating anything.

Safe to re-run: any previous run's data (identified by TEST_CUSTOMER_NAME) is
deleted first. All of this data is local/test-only -- do not point this at a
real customer's database.

Deliberately avoids `waldur_mastermind.marketplace.tests.factories` (and any
other `*.tests.factories`/`*.tests.fixtures` module): importing it drags in
`waldur_core.structure.tests.factories`, which unconditionally imports
`waldur_core.structure.tests.models` at its own top level. That module's own
app ("structure_tests") is normally only registered under test settings, so
when it's imported anyway, Django's app-label fallback folds its models
(e.g. `TestNewInstance`, a concrete `VirtualMachine`/`BaseResource` subclass)
into the *parent* `waldur_core.structure` app instead -- which genuinely is
installed here. That's enough for Django to try to cascade-delete through it
whenever a `Project` is deleted, even though its table was never migrated
into a plain (non-test-settings) dev database, raising
`relation "structure_testnewinstance" does not exist`. Every object below is
therefore built with plain `Model.objects.create(...)` calls instead.
"""

import datetime
from decimal import Decimal

from django.utils import timezone

from waldur_core.structure import models as structure_models
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.compensations import MonthlyCompensation
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing_usage import BillingUsageProcessor
from waldur_mastermind.marketplace.enums import BillingTypes, ResourceStates
from waldur_mastermind.policy import models as policy_models

TEST_CUSTOMER_NAME = "Rebill Historical Usage Test"

# Anchor to *last calendar month*, not a fixed date -- a Cost Policy's rolling
# window is measured back from today, so a hardcoded past month eventually
# ages out of every window and the policy scenarios stop demonstrating
# anything (the plain invoice/credit math below doesn't care either way).
_today = timezone.now().date()
_last_month_start = (_today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
BILLING_YEAR, BILLING_MONTH = _last_month_start.year, _last_month_start.month

UNIT_PRICE = Decimal("2.85")

# -- Scenario A: usage was wrongly inflated, corrected DOWN. Expect the
# policy to reset and the credit to be refunded. --
OLD_USAGE_A = Decimal("8629.81")  # what got billed while the invoice was mutable
NEW_USAGE_A = Decimal("764.13")  # the corrected figure, arriving after the freeze
CUSTOMER_CREDIT_VALUE = Decimal("25000")
PROJECT_CREDIT_VALUE_A = Decimal("15000")
LIMIT_COST_A = 5000  # IntegerField -- deliberately below the stale net cost

# -- Scenario B: usage was under-reported, corrected UP. Expect the credit
# correction to abort (too little left) and the policy to newly fire. --
OLD_USAGE_B = Decimal("500")
NEW_USAGE_B = Decimal("3000")
PROJECT_CREDIT_VALUE_B = Decimal("3000")  # small on purpose -- can't cover the delta
LIMIT_COST_B = 3000  # IntegerField -- below the corrected, uncompensated cost

# -- Scenario C: usage was under-reported, corrected UP, same direction as B --
# but with enough credit this time. Expect the credit correction to succeed
# and the policy to stay quiet throughout (no transition either way). --
OLD_USAGE_C = Decimal("200")
NEW_USAGE_C = Decimal("800")
PROJECT_CREDIT_VALUE_C = Decimal("5000")  # comfortably covers the delta
LIMIT_COST_C = 1000  # IntegerField -- net cost stays near zero, well under this

print(f"Cleaning up any previous run ({TEST_CUSTOMER_NAME!r})...")
structure_models.Customer.objects.filter(name=TEST_CUSTOMER_NAME).delete()

print("Building the shared customer/offering...")
customer = structure_models.Customer.objects.create(name=TEST_CUSTOMER_NAME)

category = marketplace_models.Category.objects.create(
    title="Rebill Historical Usage Test Category"
)
offering = marketplace_models.Offering.objects.create(
    customer=customer,
    category=category,
    name="Alps HPC (rebill test)",
    type="Support.OfferingTemplate",  # generic type, no plugin behavior needed here
    state=marketplace_models.Offering.States.ACTIVE,
    # Required for the "request_pausing" Cost Policy action to consider these
    # resources eligible at all -- see policy_actions._filter_resources_by_scope
    # plus the offering__plugin_options__supports_pausing=True queryset filter.
    plugin_options={"supports_pausing": True},
)
plan = marketplace_models.Plan.objects.create(offering=offering, name="Default")
offering_component = marketplace_models.OfferingComponent.objects.create(
    offering=offering,
    billing_type=BillingTypes.USAGE,
    type="node-hours",
    name="Node hours",
)
marketplace_models.PlanComponent.objects.create(
    plan=plan, component=offering_component, price=UNIT_PRICE
)


def _build_resource(project, name):
    resource = marketplace_models.Resource.objects.create(
        offering=offering,
        plan=plan,
        project=project,
        name=name,
        state=ResourceStates.OK,
    )
    # No Order needed: resource_creation_succeeded() only looks one up when
    # validate=True (the default is False), in which case a missing Order is
    # just logged and ignored -- it never touches Resource state otherwise.
    callbacks.resource_creation_succeeded(resource)
    plan_period = marketplace_models.ResourcePlanPeriod.objects.create(
        resource=resource,
        plan=plan,
        start=timezone.make_aware(datetime.datetime(BILLING_YEAR, 1, 1)),
        end=None,
    )
    return resource, plan_period


def _bill(resource, plan_period, usage_value):
    billing_period = datetime.date(BILLING_YEAR, BILLING_MONTH, 1)
    usage = marketplace_models.ComponentUsage.objects.create(
        resource=resource,
        component=offering_component,
        plan_period=plan_period,
        usage=usage_value,
        # Every real production code path derives `date` and `billing_period`
        # from the same value, so they're always in the same month -- keep
        # that invariant here too, or the frontend's "Resource usage" table
        # (which renders `date`'s month under its "Billing period" column,
        # a separate latent bug) will show a nonsensical period.
        date=timezone.make_aware(datetime.datetime(BILLING_YEAR, BILLING_MONTH, 15)),
        billing_period=billing_period,
    )
    BillingUsageProcessor._run_billing(usage, created=True)
    return usage


print("Scenario A -- building 'inflated usage, corrected down' project...")
project_a = structure_models.Project.objects.create(
    customer=customer, name="Rebill Historical Usage Test Project A"
)
resource_a, plan_period_a = _build_resource(project_a, "cluster-a-01")
usage_a = _bill(resource_a, plan_period_a, OLD_USAGE_A)

print("Scenario B -- building 'under-reported usage, corrected up' project...")
project_b = structure_models.Project.objects.create(
    customer=customer, name="Rebill Historical Usage Test Project B"
)
resource_b, plan_period_b = _build_resource(project_b, "cluster-b-01")
usage_b = _bill(resource_b, plan_period_b, OLD_USAGE_B)

print(
    "Scenario C -- building 'under-reported usage, corrected up, enough "
    "credit' project..."
)
project_c = structure_models.Project.objects.create(
    customer=customer, name="Rebill Historical Usage Test Project C"
)
resource_c, plan_period_c = _build_resource(project_c, "cluster-c-01")
usage_c = _bill(resource_c, plan_period_c, OLD_USAGE_C)

# All three resources are on the same customer invoice for the same month, so
# this one call processes them together -- cheapest-first, exactly like
# production billing -- rather than compensating each project in isolation.
invoice = invoice_models.Invoice.objects.get(
    customer=customer, year=BILLING_YEAR, month=BILLING_MONTH
)
credit = invoice_models.CustomerCredit.objects.create(
    customer=customer, value=CUSTOMER_CREDIT_VALUE
)
project_credit_a = invoice_models.ProjectCredit.objects.create(
    project=project_a, value=PROJECT_CREDIT_VALUE_A
)
project_credit_b = invoice_models.ProjectCredit.objects.create(
    project=project_b, value=PROJECT_CREDIT_VALUE_B
)
project_credit_c = invoice_models.ProjectCredit.objects.create(
    project=project_c, value=PROJECT_CREDIT_VALUE_C
)
MonthlyCompensation(customer, invoice=invoice).apply_compensations()
credit.refresh_from_db()
project_credit_a.refresh_from_db()
project_credit_b.refresh_from_db()
project_credit_c.refresh_from_db()

print(
    f"  Real compensation applied. Customer credit: {credit.value}, "
    f"project A credit: {project_credit_a.value}, "
    f"project B credit: {project_credit_b.value}, "
    f"project C credit: {project_credit_c.value}"
)

print("Freezing the invoice, as happens automatically a few days after month-end...")
invoice.set_created()
print(f"  Invoice {invoice.year}-{invoice.month:02d} finalized (state={invoice.state})")

print("Configuring Cost Policies to mirror each project's 'before' state...")
policy_a = policy_models.ProjectEstimatedCostPolicy.objects.create(
    scope=project_a,
    limit_cost=LIMIT_COST_A,
    actions="request_pausing",
    use_credit=True,
    period=invoice_models.PeriodMixin.Periods.MONTH_3,
    # This project's stale numbers already cross both gates (see the module
    # docstring's numbers) -- pre-set has_fired/paused to mirror what a real
    # evaluation would already have done, so the correction below produces a
    # visible RESET rather than a fresh, less illustrative first fire.
    has_fired=True,
)
resource_a.paused = True
resource_a.save(update_fields=["paused"])

policy_b = policy_models.ProjectEstimatedCostPolicy.objects.create(
    scope=project_b,
    limit_cost=LIMIT_COST_B,
    actions="request_pausing",
    use_credit=True,
    period=invoice_models.PeriodMixin.Periods.MONTH_3,
    # Starts clear -- the correction below is what's expected to newly fire it.
    has_fired=False,
)

policy_c = policy_models.ProjectEstimatedCostPolicy.objects.create(
    scope=project_c,
    limit_cost=LIMIT_COST_C,
    actions="request_pausing",
    use_credit=True,
    period=invoice_models.PeriodMixin.Periods.MONTH_3,
    # Starts clear and is expected to stay clear -- the correction below has
    # enough credit behind it that neither gate should ever open.
    has_fired=False,
)

# The historical usage loader "corrects" usage after the freeze --
# ComponentUsage.usage is updated directly (bypassing billing, exactly as the
# real API does once its invoice is no longer mutable), leaving the invoice
# item and its compensation stale. This is the bug rebill_historical_usage
# fixes, in all three directions at once here.
marketplace_models.ComponentUsage.objects.filter(pk=usage_a.pk).update(
    usage=NEW_USAGE_A
)
marketplace_models.ComponentUsage.objects.filter(pk=usage_b.pk).update(
    usage=NEW_USAGE_B
)
marketplace_models.ComponentUsage.objects.filter(pk=usage_c.pk).update(
    usage=NEW_USAGE_C
)
usage_a.refresh_from_db()
usage_b.refresh_from_db()
usage_c.refresh_from_db()

print()
print("=" * 78)
print("Scenario A -- usage corrected DOWN, expect the policy to RESET")
print("=" * 78)
print(f"  Resource UUID:           {resource_a.uuid.hex}")
print(f"  Resource name:           {resource_a.name}")
print(f"  Project:                 {project_a.name}")
print(f"  Billing period:          {BILLING_YEAR}-{BILLING_MONTH:02d}")
print(f"  ComponentUsage.usage:    {usage_a.usage}  (corrected)")
print(f"  Project credit balance:  {project_credit_a.value}")
print(f"  Cost Policy limit_cost:  {LIMIT_COST_A}  (has_fired=True, resource paused)")
print(
    "  Expect: cost net of compensation drops well under limit_cost, "
    "credit balance is refunded back above limit_cost -- both gates close, "
    "preview should report 'fired: True -> False' and reset the resource."
)
print()
print("=" * 78)
print("Scenario B -- usage corrected UP, expect the policy to FIRE")
print("=" * 78)
print(f"  Resource UUID:           {resource_b.uuid.hex}")
print(f"  Resource name:           {resource_b.name}")
print(f"  Project:                 {project_b.name}")
print(f"  Billing period:          {BILLING_YEAR}-{BILLING_MONTH:02d}")
print(f"  ComponentUsage.usage:    {usage_b.usage}  (corrected)")
print(f"  Project credit balance:  {project_credit_b.value}  (too small for the delta)")
print(f"  Cost Policy limit_cost:  {LIMIT_COST_B}  (has_fired=False, resource clear)")
print(
    "  Expect: the credit correction ABORTS (balance left untouched, an ERROR "
    "line printed) but the cost correction still lands on the invoice -- with "
    "no offsetting compensation, both gates open, preview should report "
    "'fired: False -> True' and list the resource as newly paused."
)
print("=" * 78)
print()
print("=" * 78)
print("Scenario C -- usage corrected UP with enough credit, expect NO transition")
print("=" * 78)
print(f"  Resource UUID:           {resource_c.uuid.hex}")
print(f"  Resource name:           {resource_c.name}")
print(f"  Project:                 {project_c.name}")
print(f"  Billing period:          {BILLING_YEAR}-{BILLING_MONTH:02d}")
print(f"  ComponentUsage.usage:    {usage_c.usage}  (corrected)")
print(
    f"  Project credit balance:  {project_credit_c.value}  (comfortably covers the delta)"
)
print(f"  Cost Policy limit_cost:  {LIMIT_COST_C}  (has_fired=False, resource clear)")
print(
    "  Expect: the credit correction SUCCEEDS -- no ERROR line, credit reduced "
    "by exactly the delta -- and the corrected cost is fully compensated again "
    "(net cost stays near zero), so both gates stay closed. Preview should "
    "report 'fired: False -> False' (no transition) with no resource listed."
)
print("=" * 78)
print()
print("Verify with (outside the shell) -- dry run is the default, no --execute needed")
print("to see the policy-impact preview; add -v 2 for full debug-level detail:")
print(f"  uv run waldur rebill_historical_usage --resource {resource_a.uuid.hex}")
print(f"  uv run waldur rebill_historical_usage --resource {resource_b.uuid.hex}")
print(f"  uv run waldur rebill_historical_usage --resource {resource_c.uuid.hex}")
print(
    f"  uv run waldur rebill_historical_usage --offering {offering.uuid.hex}"
    "  # all three at once"
)
print()
print("Once the preview looks right, re-run the same command with --execute added.")
print(
    "SLURM Periodic Usage Policy is deliberately not part of this scenario -- "
    "it never reacts to rebill_historical_usage regardless of setup, since it "
    "only reacts to ComponentUsage changes and has no credit awareness at all."
)
print()
print(
    f"To tear down this scenario later (also done automatically on next run): "
    f"Customer.objects.filter(name={TEST_CUSTOMER_NAME!r}).delete()"
)
