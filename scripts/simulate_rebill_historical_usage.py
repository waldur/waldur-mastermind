"""Simulate the "historical usage backfill leaves the invoice stale" incident
that `rebill_historical_usage` is meant to fix, so it can be exercised
manually against a local Waldur instance.

Intended to be run inside `waldur shell` (Django's interactive shell), e.g.:

    uv run waldur shell < scripts/simulate_rebill_historical_usage.py

or by pasting its contents into an interactive `uv run waldur shell` session.

It creates a self-contained customer/project/offering/resource, bills usage
for a past month while the invoice is still mutable, configures a customer
credit and lets the real compensation engine run against it (reproducing a
resource that has BOTH a cost item and a compensation item sharing the same
`details["offering_component_type"]` -- the exact ambiguity a production
resource was found to have), freezes the invoice, then corrects the
underlying `ComponentUsage.usage` out-of-band -- mirroring exactly what
`waldur_site_load_historical_usage` does when it runs against an
already-finalized month. The invoice item and compensation are left stale,
which is the bug `rebill_historical_usage` fixes.

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

TEST_CUSTOMER_NAME = "Rebill Historical Usage Test"
BILLING_YEAR, BILLING_MONTH = 2023, 12
OLD_USAGE = Decimal("8629.81")  # what got billed while the invoice was mutable
NEW_USAGE = Decimal("764.13")  # the corrected figure, arriving after the freeze
UNIT_PRICE = Decimal("2.85")
CREDIT_VALUE = Decimal("100000")
# Mirrors the real customer, whose credit was allocated both at the
# organization level (CustomerCredit) AND down to a specific project
# (ProjectCredit) -- the two draw down in lockstep during compensation, not
# independently, so both need to exist for rebill_historical_usage's
# project-credit correction branch to actually be exercised.
PROJECT_CREDIT_VALUE = Decimal("85000")

print(f"Cleaning up any previous run ({TEST_CUSTOMER_NAME!r})...")
structure_models.Customer.objects.filter(name=TEST_CUSTOMER_NAME).delete()

print("Building the scenario...")
customer = structure_models.Customer.objects.create(name=TEST_CUSTOMER_NAME)
project = structure_models.Project.objects.create(
    customer=customer, name="Rebill Historical Usage Test Project"
)

category = marketplace_models.Category.objects.create(
    title="Rebill Historical Usage Test Category"
)
offering = marketplace_models.Offering.objects.create(
    customer=customer,
    category=category,
    name="Alps HPC (rebill test)",
    type="Support.OfferingTemplate",  # generic type, no plugin behavior needed here
    state=marketplace_models.Offering.States.ACTIVE,
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

resource = marketplace_models.Resource.objects.create(
    offering=offering,
    plan=plan,
    project=project,
    name="test-cluster-01",
    state=ResourceStates.OK,
)
# No Order needed: resource_creation_succeeded() only looks one up when
# validate=True (the default is False), in which case a missing Order is
# just logged and ignored -- it never touches Resource state otherwise.
callbacks.resource_creation_succeeded(resource)

# Open-ended plan period predating the billing month -- mirrors the shape
# found on the real resource this bug was diagnosed against.
plan_period = marketplace_models.ResourcePlanPeriod.objects.create(
    resource=resource,
    plan=plan,
    start=timezone.make_aware(datetime.datetime(BILLING_YEAR, 1, 1)),
    end=None,
)

# 1. Bill the (initially correct, as far as anyone knew) usage while the
#    invoice is still mutable -- exactly what live SLURM/DWDI reporting does.
billing_period = datetime.date(BILLING_YEAR, BILLING_MONTH, 1)
usage = marketplace_models.ComponentUsage.objects.create(
    resource=resource,
    component=offering_component,
    plan_period=plan_period,
    usage=OLD_USAGE,
    # Every real production code path derives `date` and `billing_period`
    # from the same value, so they're always in the same month -- keep
    # that invariant here too, or the frontend's "Resource usage" table
    # (which renders `date`'s month under its "Billing period" column,
    # a separate latent bug) will show a nonsensical period.
    date=timezone.make_aware(datetime.datetime(BILLING_YEAR, BILLING_MONTH, 15)),
    billing_period=billing_period,
)
BillingUsageProcessor._run_billing(usage, created=True)

invoice = invoice_models.Invoice.objects.get(
    customer=customer, year=BILLING_YEAR, month=BILLING_MONTH
)
cost_item = invoice.items.get(
    resource=resource, details__offering_component_type="node-hours"
)
print(
    f"  Billed {OLD_USAGE} node-hours -> invoice item price {cost_item.price} "
    f"(invoice {invoice.year}-{invoice.month:02d}, still {invoice.state})"
)

# 2. Configure a customer credit AND a project credit, then let the real
#    compensation engine run. This produces a compensation item for the SAME
#    resource/component, sharing details["offering_component_type"] with the
#    cost item, and draws down both balances together.
credit = invoice_models.CustomerCredit.objects.create(
    customer=customer, value=CREDIT_VALUE
)
project_credit = invoice_models.ProjectCredit.objects.create(
    project=project, value=PROJECT_CREDIT_VALUE
)
MonthlyCompensation(customer, invoice=invoice).apply_compensations()
credit.refresh_from_db()
project_credit.refresh_from_db()

compensation_item = invoice.items.filter(
    resource=resource,
    credit=credit,
    details__offering_component_type="node-hours",
).first()
print(
    f"  Compensation item unit_price={compensation_item.unit_price if compensation_item else None}, "
    f"customer credit balance now {credit.value}, "
    f"project credit balance now {project_credit.value}"
)

# 3. Freeze the invoice, as happens automatically a few days after month-end.
invoice.set_created()
print(f"  Invoice {invoice.year}-{invoice.month:02d} finalized (state={invoice.state})")

# 4. The historical usage loader "corrects" usage after the freeze --
#    ComponentUsage.usage is updated directly (bypassing billing, exactly as
#    the real API does once its invoice is no longer mutable), leaving the
#    invoice item and its compensation stale. This is the bug.
marketplace_models.ComponentUsage.objects.filter(pk=usage.pk).update(usage=NEW_USAGE)
usage.refresh_from_db()
cost_item.refresh_from_db()

expected_delta = NEW_USAGE * UNIT_PRICE - OLD_USAGE * UNIT_PRICE  # negative: a refund

print()
print("=" * 78)
print("Scenario ready -- invoice item and compensation are now STALE.")
print(f"  Resource UUID:          {resource.uuid.hex}")
print(f"  Resource name:          {resource.name}")
print(f"  Offering:                {offering.name}")
print(f"  Billing period:          {BILLING_YEAR}-{BILLING_MONTH:02d}")
print(f"  ComponentUsage.usage:    {usage.usage}  (corrected)")
print(
    f"  Cost item price:        {cost_item.price}  (stale -- reflects {OLD_USAGE}, should become {NEW_USAGE * UNIT_PRICE})"
)
if compensation_item:
    print(f"  Compensation unit_price: {compensation_item.unit_price}  (stale)")
print(f"  Customer credit balance: {credit.value}")
print(f"  Project credit balance:  {project_credit.value}")
print(f"  Invoice state:           {invoice.state}")
print("=" * 78)
print()
print("Verify with (outside the shell):")
print(
    f"  uv run waldur rebill_historical_usage --resource {resource.uuid.hex}"
    "  # dry run by default"
)
print(
    f"  uv run waldur rebill_historical_usage --resource {resource.uuid.hex} --execute"
)
print()
print("Expected after a real (--execute) invocation:")
print(f"  - cost item price          -> {NEW_USAGE * UNIT_PRICE}")
print(f"  - compensation price       -> -{NEW_USAGE * UNIT_PRICE}")
print(f"  - customer credit balance  -> {credit.value - expected_delta}")
print(f"  - project credit balance   -> {project_credit.value - expected_delta}")
print("  - invoice state            -> created (re-finalized)")
print()
print(
    f"To tear down this scenario later (also done automatically on next run): "
    f"Customer.objects.filter(name={TEST_CUSTOMER_NAME!r}).delete()"
)
