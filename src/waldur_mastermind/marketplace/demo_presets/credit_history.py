"""Historical credit consumption for demo presets.

Invoice items and credit compensations are **not** authored as fixture rows.
Past months are billed and then run through the real
``MonthlyCompensation.apply_compensations()`` so the resulting data obeys every
invariant the production flow guarantees:

* a compensation never exceeds the cost of the item it offsets;
* the minimal-consumption draw reduces ``credit.value`` and produces **no**
  invoice item, so it is invisible to anything reading invoice items alone;
* a credit whose end date has passed is zeroed before the month is
  compensated, exactly as finalization does it, so the balance it gave up is
  recorded as forfeiture rather than quietly surviving;
* ``credit.value`` reconciles to granted minus compensations minus floor draws.

Hand-authored credit fixtures have historically violated all three — which made
demo data look healthier than production and hid a real defect where compensation
items were never attributed to their project. Generating through the live code
path keeps the demo honest as that code evolves.
"""

import calendar
import datetime
import decimal
import logging
from io import StringIO

from django.db import transaction
from django.utils import timezone

from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices import compensations
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import tasks as invoice_tasks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import ResourceStates

logger = logging.getLogger(__name__)

DEFAULT_MONTHS = 6

# Usage for each historical month, as a fraction of the credit's expected
# consumption. Fixed rather than random so a preset loads identically every
# time. The cycle deliberately walks the states the credit panels must render:
#   1.05 — above the expected draw
#   0.90 — comfortably above the minimal floor
#   0.35 — below the floor: small compensation plus an invisible tail
#   1.00 — compensation ~= incurred, so the invoice nets to about zero
#   0.75 — between floor and expected
#   0.20 — deep under the floor, the "losing credit" case
USAGE_PATTERN = [
    decimal.Decimal("1.05"),
    decimal.Decimal("0.90"),
    decimal.Decimal("0.35"),
    decimal.Decimal("1.00"),
    decimal.Decimal("0.75"),
    decimal.Decimal("0.20"),
]


def _previous_months(count: int) -> list[tuple[int, int]]:
    """The `count` months before the current one, oldest first."""
    today = timezone.localtime(timezone.now()).date()
    months = []
    year, month = today.year, today.month
    for _ in range(count):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        months.append((year, month))
    return list(reversed(months))


def _monthly_target(credit) -> decimal.Decimal:
    """Cost that represents a full month of expected consumption."""
    expected = decimal.Decimal(credit.expected_consumption or 0)
    if expected > 0:
        return expected
    value = decimal.Decimal(credit.value or 0)
    return value / 12 if value > 0 else decimal.Decimal("0")


def _billable_resources(project) -> list:
    return list(
        marketplace_models.Resource.objects.filter(
            project=project, state=ResourceStates.OK
        ).select_related("offering")
    )


def generate_credit_history(
    months: int = DEFAULT_MONTHS,
    stdout: StringIO = None,
    patterns: dict | None = None,
):
    """Bill and compensate the past `months` months for every credited customer.

    `patterns` lets a preset give individual projects their own consumption
    shape, keyed by project UUID:

        {"<project uuid hex>": {"months": [0.2, 0.15], "current": 0.15}}

    `months` is cycled oldest-first for the past months, and `current` bills the
    month in progress pro rata for the days elapsed, so a project can sit
    mid-month on any pace. Projects with no entry keep the shared USAGE_PATTERN,
    which is what every existing preset relies on.

    A project may also declare cost its credit does not cover:

        {"<uuid>": {"months": [...], "current": 1.0,
                    "uncovered": {"months": [...], "current": 4.8}}}

    A credit covers the offerings named on the organization balance and no
    others, so a project can carry real cost no compensation will ever touch.
    `uncovered` bills the resources outside that list at its own fractions,
    leaving `months`/`current` to describe the covered ones. Without the key the
    project is billed as one, so no existing preset changes.

    Returns the number of (customer, month) pairs processed.
    """
    write = stdout.write if stdout else (lambda _message: None)
    patterns = patterns or {}
    processed = 0

    customer_credits = (
        invoice_models.CustomerCredit.objects.select_related("customer")
        .order_by("id")
        .all()
    )
    if not customer_credits:
        return 0

    for customer_credit in customer_credits:
        # One customer must not take the rest down with it. A preset may hold a
        # deliberately impossible organization on purpose — the capped and
        # exhausted scenarios allocate more to their projects than the balance
        # holds, which is the state they exist to demonstrate, and which makes
        # ProjectCredit.save() refuse. Before this, the first such customer
        # aborted the whole run, and which customers kept their history came
        # down to the order the queryset happened to return.
        try:
            processed += _generate_for_customer(
                customer_credit, months, patterns, write
            )
        except Exception as exc:
            logger.warning(
                "Skipped credit history for %s: %s",
                customer_credit.customer.name,
                exc,
            )

    return processed


def _generate_for_customer(customer_credit, months, patterns, write) -> int:
    """Bill and compensate `months` months for one credited customer.

    Returns the number of months that produced an invoice.
    """
    customer = customer_credit.customer
    project_credits = {
        project_credit.project_id: project_credit
        for project_credit in invoice_models.ProjectCredit.objects.filter(
            project__customer=customer
        ).select_related("project")
    }

    processed = 0
    for index, (year, month) in enumerate(_previous_months(months)):
        fraction = USAGE_PATTERN[index % len(USAGE_PATTERN)]
        # Finalization expires overdue credit before it compensates the
        # month, and the order matters: a credit that lapsed earlier in the
        # simulated history must not go on paying for usage after its end
        # date. Without this the generator produced histories no real
        # deployment could have, and no `expiry` row ever existed in demo
        # data — leaving the dashboards' forfeiture figure untestable.
        _expire_overdue(datetime.date(year, month, 1))
        with transaction.atomic():
            invoice, _ = invoice_models.Invoice.objects.get_or_create(
                customer=customer, year=year, month=month
            )
            if invoice.state != invoice_models.Invoice.States.PENDING:
                continue

            created_items = _bill_month(
                invoice,
                customer,
                project_credits,
                customer_credit,
                fraction,
                patterns=patterns,
                month_index=index,
            )
            if not created_items:
                continue

            compensations.MonthlyCompensation(
                customer, invoice=invoice
            ).apply_compensations()
            invoice.set_created()

        processed += 1

    _expire_overdue(timezone.localtime(timezone.now()).date().replace(day=1))

    if patterns:
        _bill_current_month(customer, project_credits, customer_credit, patterns)

    write(f"Generated {months} months of credit history for {customer.name}\n")
    return processed


def _expire_overdue(effective_date) -> None:
    """Zero credit whose end date preceded this month, as finalization does.

    Never dated into the future: the task refuses a future effective date, and
    the current month's first day is the furthest the simulated clock reaches.
    """
    today = timezone.localtime(timezone.now()).date()
    invoice_tasks.set_to_zero_overdue_credits(min(effective_date, today))


def _bill_current_month(customer, project_credits, customer_credit, patterns) -> int:
    """Charge the month in progress, pro rata for the days elapsed.

    Only projects that declare a `current` fraction are billed, and nothing is
    compensated: mid-month invoices are still pending in production, so
    compensation items do not exist yet and the dashboards read the gross cost.
    """
    today = timezone.localtime(timezone.now()).date()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    elapsed = decimal.Decimal(today.day) / decimal.Decimal(days_in_month)
    created = 0

    with transaction.atomic():
        invoice, _ = invoice_models.Invoice.objects.get_or_create(
            customer=customer, year=today.year, month=today.month
        )
        covered_offering_ids = _covered_offering_ids(customer_credit)
        for project in customer.projects.all():
            pattern = patterns.get(project.uuid.hex)
            if not pattern or pattern.get("current") is None:
                continue
            credit = project_credits.get(project.id) or customer_credit
            fraction = decimal.Decimal(str(pattern["current"])) * elapsed
            uncovered_current = (pattern.get("uncovered") or {}).get("current")
            uncovered_fraction = (
                decimal.Decimal(str(uncovered_current)) * elapsed
                if uncovered_current is not None
                else None
            )
            created += _bill_month_for_project(
                invoice,
                project,
                credit,
                fraction,
                suffix="usage so far",
                uncovered_fraction=uncovered_fraction,
                covered_offering_ids=covered_offering_ids,
            )

    return created


def _bill_month(
    invoice,
    customer,
    project_credits,
    customer_credit,
    fraction,
    patterns=None,
    month_index=0,
) -> int:
    """Create usage invoice items for one month. Returns the item count."""
    created = 0
    covered_offering_ids = _covered_offering_ids(customer_credit)
    for project in customer.projects.all():
        credit = project_credits.get(project.id) or customer_credit
        created += _bill_month_for_project(
            invoice,
            project,
            credit,
            _project_fraction(project, patterns, month_index, fraction),
            uncovered_fraction=_uncovered_fraction(project, patterns, month_index),
            covered_offering_ids=covered_offering_ids,
        )

    return created


def _project_fraction(project, patterns, month_index, default) -> decimal.Decimal:
    """The project's own consumption shape for this month, or the shared one."""
    pattern = (patterns or {}).get(project.uuid.hex)
    shape = pattern.get("months") if pattern else None
    if not shape:
        return default
    return decimal.Decimal(str(shape[month_index % len(shape)]))


def _uncovered_fraction(project, patterns, month_index) -> decimal.Decimal | None:
    """Cost this project carries outside what the credit covers, if it declares any.

    None means the project makes no such distinction, and the whole of it is
    billed as one — which is what every preset without an `uncovered` block
    wants, and what this generator did before the key existed.
    """
    pattern = (patterns or {}).get(project.uuid.hex) or {}
    shape = (pattern.get("uncovered") or {}).get("months")
    if not shape:
        return None
    return decimal.Decimal(str(shape[month_index % len(shape)]))


def _covered_offering_ids(customer_credit) -> set:
    """Offerings the credit covers. Empty means unrestricted, as it does in
    MonthlyCompensation — so a credit naming none covers everything and no
    project under it can have uncovered cost."""
    return set(customer_credit.offerings.values_list("id", flat=True))


def _bill_month_for_project(
    invoice,
    project,
    credit,
    fraction,
    suffix="usage",
    uncovered_fraction=None,
    covered_offering_ids=None,
) -> int:
    """Spread one month's consumption across the project's billable resources.

    `uncovered_fraction` splits the project in two: resources whose offering the
    organization credit covers are billed at `fraction`, and the rest at
    `uncovered_fraction`. Both are fractions of the same monthly target, so a
    preset can say "this project spends its plan on compute and four times that
    again on storage the credit does not cover" in one legible pair of numbers.

    Left as None — which every preset but the partly-covered scenario does — the
    whole project is billed together at `fraction`, as before.

    Items are saved individually (not bulk-created) so the denormalising
    post_save handler populates project_name/project_uuid, exactly as the
    marketplace billing path does.
    """
    resources = _billable_resources(project)
    if not resources:
        return 0

    target = _monthly_target(credit)
    if uncovered_fraction is None:
        groups = [(resources, fraction)]
    else:
        covered_offering_ids = covered_offering_ids or set()
        groups = [
            ([r for r in resources if r.offering_id in covered_offering_ids], fraction),
            (
                [r for r in resources if r.offering_id not in covered_offering_ids],
                uncovered_fraction,
            ),
        ]

    created = 0
    for group, group_fraction in groups:
        if not group:
            continue
        group_target = target * group_fraction
        if group_target <= 0:
            continue

        per_resource = (group_target / len(group)).quantize(decimal.Decimal("0.01"))
        if per_resource <= 0:
            continue

        for resource in group:
            invoice_models.InvoiceItem.objects.create(
                invoice=invoice,
                project=project,
                resource=resource,
                unit_price=per_resource,
                quantity=1,
                unit=Units.QUANTITY,
                name=f"{resource.name} {suffix}",
                details={"demo_generated": True},
            )
        created += len(group)

    return created
