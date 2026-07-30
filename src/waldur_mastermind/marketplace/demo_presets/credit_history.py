"""Historical credit consumption for demo presets.

Invoice items and credit compensations are **not** authored as fixture rows.
Past months are billed and then run through the real
``MonthlyCompensation.apply_compensations()`` so the resulting data obeys every
invariant the production flow guarantees:

* a compensation never exceeds the cost of the item it offsets;
* the minimal-consumption draw reduces ``credit.value`` and produces **no**
  invoice item, so it is invisible to anything reading invoice items alone;
* ``credit.value`` reconciles to granted minus compensations minus floor draws.

Hand-authored credit fixtures have historically violated all three — which made
demo data look healthier than production and hid a real defect where compensation
items were never attributed to their project. Generating through the live code
path keeps the demo honest as that code evolves.
"""

import decimal
import logging
from io import StringIO

from django.db import transaction
from django.utils import timezone

from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices import compensations
from waldur_mastermind.invoices import models as invoice_models
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


def generate_credit_history(months: int = DEFAULT_MONTHS, stdout: StringIO = None):
    """Bill and compensate the past `months` months for every credited customer.

    Returns the number of (customer, month) pairs processed.
    """
    write = stdout.write if stdout else (lambda _message: None)
    processed = 0

    customer_credits = invoice_models.CustomerCredit.objects.select_related(
        "customer"
    ).all()
    if not customer_credits:
        return 0

    for customer_credit in customer_credits:
        customer = customer_credit.customer
        project_credits = {
            project_credit.project_id: project_credit
            for project_credit in invoice_models.ProjectCredit.objects.filter(
                project__customer=customer
            ).select_related("project")
        }

        for index, (year, month) in enumerate(_previous_months(months)):
            fraction = USAGE_PATTERN[index % len(USAGE_PATTERN)]
            with transaction.atomic():
                invoice, _ = invoice_models.Invoice.objects.get_or_create(
                    customer=customer, year=year, month=month
                )
                if invoice.state != invoice_models.Invoice.States.PENDING:
                    continue

                created_items = _bill_month(
                    invoice, customer, project_credits, customer_credit, fraction
                )
                if not created_items:
                    continue

                compensations.MonthlyCompensation(
                    customer, invoice=invoice
                ).apply_compensations()
                invoice.set_created()

            processed += 1

        write(
            f"Generated {months} months of credit history for {customer.name}\n"
            if stdout
            else ""
        )

    return processed


def _bill_month(invoice, customer, project_credits, customer_credit, fraction) -> int:
    """Create usage invoice items for one month. Returns the item count.

    Items are saved individually (not bulk-created) so the denormalising
    post_save handler populates project_name/project_uuid, exactly as the
    marketplace billing path does.
    """
    created = 0
    for project in customer.projects.all():
        resources = _billable_resources(project)
        if not resources:
            continue

        credit = project_credits.get(project.id) or customer_credit
        target = _monthly_target(credit) * fraction
        if target <= 0:
            continue

        per_resource = (target / len(resources)).quantize(decimal.Decimal("0.01"))
        if per_resource <= 0:
            continue

        for resource in resources:
            invoice_models.InvoiceItem.objects.create(
                invoice=invoice,
                project=project,
                resource=resource,
                unit_price=per_resource,
                quantity=1,
                unit=Units.QUANTITY,
                name=f"{resource.name} usage",
                details={"demo_generated": True},
            )
            created += 1

    return created
