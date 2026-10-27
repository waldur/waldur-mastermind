"""A project credit paying the cost of its project's earlier invoices.

The monthly compensation run only ever pays the month it closes, so cost that
no credit covered stays uncovered for good, and a period-Total cost policy
keeps counting it however much credit is added later. With
`ProjectCredit.pay_earlier_invoices` (staff only), what is left of the
allocation also pays that cost, oldest month first.

A payment is a credit compensation like any other, written on an invoice that
is still open, with `compensated_period` naming the month it pays; every
cost-by-month figure counts it there (`utils.month_q`). Payments are written
once and never rolled back: `MonthlyCompensation.clear_compensations` leaves
them alone, and what a month still needs is worked out from what stands on the
invoices (`earlier_months`), so running it again pays nothing twice.
"""

import dataclasses
import datetime
import decimal
import logging

from django.db import transaction
from django.db.models import Q

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_mastermind.common.enums import Units

from . import compensations, ledger, models
from .audit import skip_credit_audit

logger = logging.getLogger(__name__)

ZERO = decimal.Decimal(0)

#: Invoices whose month is over and billed. Pending ones are still compensated
#: by their own month's run, and a canceled invoice is not a bill.
CLOSED_STATES = (models.Invoice.States.CREATED, models.Invoice.States.PAID)


@dataclasses.dataclass
class EarlierMonth:
    """What a closed month cost the project, and how much of it credit paid."""

    period: datetime.date
    #: The month's own invoice, closed.
    invoice: object
    #: The cost credit could pay: the project's items on offerings the credit
    #: covers, net of their volume discounts, before tax.
    cost: decimal.Decimal
    #: What paid for it: the credit compensation on its invoice, later payments
    #: of the month, and staff refunds of its items written on later invoices.
    covered: decimal.Decimal
    #: The cost items, for spreading a payment over them.
    items: list = dataclasses.field(default_factory=list, repr=False)
    #: Per cost item uuid, what is already known to pay it: its discounts and
    #: the compensations linked to it.
    paid_by_item: dict = dataclasses.field(default_factory=dict, repr=False)

    @property
    def uncovered(self) -> decimal.Decimal:
        return max(ZERO, self.cost - self.covered)


def _month(invoice) -> datetime.date:
    return datetime.date(invoice.year, invoice.month, 1)


def earlier_months(
    project_credit, start=None, end=None, before=None
) -> list[EarlierMonth]:
    """The project's closed months from `start` to `end` (inclusive, months
    named by their first day) and before `before`, oldest first.

    Worked out from every item that stands, rather than from compensation
    items alone, because those are not reliably marked: `credit` goes null when
    an organization credit is deleted, and `is_compensation` /
    `compensation_of_item` are younger than many invoices. Compensations and
    discounts carry the resource of the item they offset, so summing the
    project's items on covered offerings nets them out either way.

    Items are taken by `project`, not `resource__project`: a resource moved to
    another project takes its resource with it, but its closed invoices keep
    the project they were billed to, and the new project must not pay them.
    """
    project = project_credit.project
    credit = models.CustomerCredit.objects.filter(customer=project.customer).first()
    if not credit:
        return []

    invoices = models.Invoice.objects.filter(
        customer=project.customer, state__in=CLOSED_STATES
    ).order_by("year", "month")
    if start:
        invoices = invoices.filter(
            Q(year__gt=start.year) | Q(year=start.year, month__gte=start.month)
        )
    if end:
        invoices = invoices.filter(
            Q(year__lt=end.year) | Q(year=end.year, month__lte=end.month)
        )
    if before:
        invoices = invoices.filter(
            Q(year__lt=before.year) | Q(year=before.year, month__lt=before.month)
        )
    invoices = list(invoices)
    if not invoices:
        return []

    offering_ids = set(credit.offerings.values_list("id", flat=True))
    items = models.InvoiceItem.objects.filter(
        invoice__in=invoices,
        project=project,
        resource__isnull=False,
        compensated_period__isnull=True,
    )
    if offering_ids:
        items = items.filter(resource__offering_id__in=offering_ids)
    items = list(items.order_by("pk"))

    months = {
        _month(invoice): EarlierMonth(
            period=_month(invoice), invoice=invoice, cost=ZERO, covered=ZERO
        )
        for invoice in invoices
    }
    month_of_invoice = {invoice.pk: _month(invoice) for invoice in invoices}
    month_of_item = {}
    for item in items:
        details = item.details or {}
        if details.get("original_invoice_item_uuid"):
            # A staff refund of an earlier item (`create_compensation`) is
            # counted against the month it refunds, below.
            continue
        month = months[month_of_invoice[item.invoice_id]]
        price = item.price
        if details.get("is_discount"):
            month.cost += price
            target = details.get("discount_of_item")
            if target:
                month.paid_by_item[target] = (
                    month.paid_by_item.get(target, ZERO) - price
                )
        elif price >= 0:
            month.cost += price
            month.items.append(item)
            month_of_item[item.uuid.hex] = month
        else:
            month.covered -= price
            target = details.get("compensation_of_item")
            if target:
                month.paid_by_item[target] = (
                    month.paid_by_item.get(target, ZERO) - price
                )

    # Payments of these months already made, wherever they were written.
    for payment in models.InvoiceItem.objects.filter(
        project=project, compensated_period__in=list(months)
    ):
        month = months[payment.compensated_period]
        month.covered -= payment.price
        target = (payment.details or {}).get("compensation_of_item")
        if target:
            month.paid_by_item[target] = (
                month.paid_by_item.get(target, ZERO) - payment.price
            )

    # Staff refunds of these months' items written on later invoices. A refund
    # copies its item, project included; narrowed by project and sign first,
    # since nothing indexes the details key.
    if month_of_item:
        for refund in models.InvoiceItem.objects.filter(
            project=project,
            unit_price__lt=0,
            details__original_invoice_item_uuid__in=list(month_of_item),
        ):
            target = refund.details["original_invoice_item_uuid"]
            month = month_of_item[target]
            month.covered -= refund.price
            month.paid_by_item[target] = month.paid_by_item.get(target, ZERO) - (
                refund.price
            )

    return list(months.values())


def live_available(project_credit, customer_credit, invoice) -> decimal.Decimal:
    """What the allocation can pay now, after this month's usage so far.

    The stored balances fall only when the month is closed, so this month's
    usage is taken off them, from a simulation of the month that starts from
    its opening balance; the arithmetic is the cost policies' live balance.
    A payment of an earlier invoice is not a draw of the month: the
    simulation does not add it back, so the stored balance it lowered stands.
    """
    project = project_credit.project
    simulation = compensations.MonthlyCompensation(
        project.customer, invoice=invoice, restore_written=True
    )
    customer_balance = (
        customer_credit.value
        + simulation.customer_restored_draw
        - simulation.customer_usage_draw
    )
    project_balance = (
        project_credit.value
        + simulation.get_project_restored_draw(project)
        - simulation.get_project_usage_draw(project)
    )
    return max(ZERO, min(customer_balance, project_balance))


@dataclasses.dataclass
class PlannedPayment:
    """One compensation a payment writes: which month and item it pays, how
    much, and the invoice it goes on."""

    period: datetime.date
    item: object
    amount: decimal.Decimal
    #: Where it is written: the open invoice, or the paid month's own. None
    #: in a preview made while the current month has no invoice yet.
    invoice: object
    #: The month of that invoice.
    invoice_period: datetime.date


@dataclasses.dataclass
class PaymentPlan:
    """What paying earlier invoices does: the compensations it writes, and the
    balances before and after."""

    compensate_in: str
    payments: list
    project_credit_before: decimal.Decimal
    customer_credit_before: decimal.Decimal

    @property
    def total(self) -> decimal.Decimal:
        return sum((payment.amount for payment in self.payments), ZERO)

    @property
    def project_credit_after(self) -> decimal.Decimal:
        return self.project_credit_before - self.total

    @property
    def customer_credit_after(self) -> decimal.Decimal:
        return self.customer_credit_before - self.total


def _payments(months, available, compensate_in, target_invoice, target_period):
    """Spread up to `available` over the months' uncovered cost, oldest month
    first, and within a month over its items, most expensive first.

    Most expensive first because a compensation run paid each month cheapest
    first, so what it left uncovered sits on the expensive items. Each item is
    capped at what is not known to be paid for it, and the month at what it
    still needs, so an item whose compensation carries no link is never paid
    beyond its month.
    """
    incurred = compensate_in == models.ProjectCredit.CompensateIn.INCURRED_MONTH
    payments = []
    left = available
    for month in months:
        need = min(month.uncovered, left)
        for item in sorted(month.items, key=lambda i: (-i.price, i.pk)):
            if need <= 0:
                break
            open_on_item = item.price - month.paid_by_item.get(item.uuid.hex, ZERO)
            amount = min(need, open_on_item)
            if amount <= 0:
                continue
            payments.append(
                PlannedPayment(
                    period=month.period,
                    item=item,
                    amount=amount,
                    invoice=month.invoice if incurred else target_invoice,
                    invoice_period=month.period if incurred else target_period,
                )
            )
            need -= amount
            left -= amount
        if left <= 0:
            break
    return payments


def plan(
    project_credit,
    customer_credit,
    available,
    target_invoice,
    target_period,
    compensate_in=None,
    period=None,
) -> PaymentPlan:
    """What a payment from `available` would write, without writing it.

    Months before `target_period` are paid; with compensate_in
    `current_month` the compensations go on `target_invoice`, with
    `incurred_month` on each paid month's own invoice. `period` is a
    (from, until) pair to plan with instead of the credit's own bounds.
    """
    compensate_in = compensate_in or project_credit.earlier_invoices_compensate_in
    start, end = period or (
        project_credit.earlier_invoices_from,
        project_credit.earlier_invoices_until,
    )
    payments = []
    if available > 0:
        months = earlier_months(
            project_credit, start=start, end=end, before=target_period
        )
        payments = _payments(
            months, available, compensate_in, target_invoice, target_period
        )
    return PaymentPlan(
        compensate_in=compensate_in,
        payments=payments,
        project_credit_before=project_credit.value,
        customer_credit_before=customer_credit.value,
    )


def _write(project_credit, customer_credit, payment_plan) -> PaymentPlan:
    """Write a plan's compensations and lower both balances by its total.

    Each invoice the plan writes on gets its own credit movement, ledgered to
    that invoice and its month, so the ledger says which month each draw went
    to. The caller holds the row locks on both credits.
    """
    if not payment_plan.payments:
        return payment_plan
    project = project_credit.project
    by_invoice = {}
    for payment in payment_plan.payments:
        by_invoice.setdefault(payment.invoice.pk, []).append(payment)

    for payments in by_invoice.values():
        invoice = payments[0].invoice
        items = []
        for payment in payments:
            details = dict(payment.item.details or {})
            details["is_compensation"] = True
            details["compensation_of_item"] = payment.item.uuid.hex
            items.append(
                models.InvoiceItem(
                    invoice=invoice,
                    unit_price=-payment.amount,
                    quantity=1,
                    unit=Units.QUANTITY,
                    credit=customer_credit,
                    compensated_period=payment.period,
                    name=f"Credit compensation for {payment.period:%Y-%m}. {payment.item}",
                    resource=payment.item.resource,
                    project=project,
                    # Normally set by a post_save handler, which bulk_create
                    # does not fire; the project costs endpoint filters on them.
                    project_name=project.name,
                    project_uuid=project.uuid.hex,
                    details=details,
                )
            )
        models.InvoiceItem.objects.bulk_create(items)

        total = sum((payment.amount for payment in payments), ZERO)
        paid_months = sorted({payment.period for payment in payments})
        with (
            skip_credit_audit(),
            ledger.credit_transaction_type(
                models.CreditTransaction.Types.HISTORICAL_COMPENSATION,
                reference=invoice,
                billing_period=_month(invoice),
                # The project too: the organization credit's row carries no
                # project of its own, and several projects may pay.
                comment=f"Pays the uncovered cost of project {project.name} in "
                + ", ".join(f"{period:%Y-%m}" for period in paid_months),
            ),
        ):
            project_credit.value -= total
            project_credit.save(update_fields=["value"])
            customer_credit.value -= total
            customer_credit.save(update_fields=["value"])

        for item in items:
            event_logger.emit(
                "Reduction of {project_name} credit by {consumption} due to compensation of invoice item {invoice_item}.",
                event_type=EventType.REDUCTION_OF_PROJECT_CREDIT,
                event_context={
                    "consumption": item.unit_price,
                    "customer": project.customer,
                    "project": project,
                    "invoice_item": str(item),
                    "credit_balance": int(customer_credit.value),
                    "project_credit_balance": int(project_credit.value),
                },
                scopes=[project.customer, project],
            )
            # bulk_create sends no post_save, so the handler's event is not sent.
            event_logger.emit(
                f"Invoice item {item.name} has been created.",
                event_type=EventType.INVOICE_ITEM_CREATED,
                event_context={"invoice_item": item},
                scopes=[invoice.customer, invoice],
            )
        logger.info(
            "%s paid %s of earlier invoices (%s) onto invoice %s",
            project_credit,
            total,
            ", ".join(f"{period:%Y-%m}" for period in paid_months),
            invoice.pk,
        )
    return payment_plan


def _current_month() -> datetime.date:
    return datetime.date.today().replace(day=1)


def preview(project_credit, compensate_in=None, period=None) -> PaymentPlan:
    """What paying now would write, with nothing written and no invoice
    created: the dry run of `pay_earlier_invoices`. `compensate_in` and
    `period` preview settings that are not saved yet."""
    customer = project_credit.project.customer
    customer_credit = models.CustomerCredit.objects.filter(customer=customer).first()
    if not customer_credit:
        return PaymentPlan(
            compensate_in or project_credit.earlier_invoices_compensate_in,
            [],
            project_credit.value,
            ZERO,
        )
    today = _current_month()
    invoice = models.Invoice.objects.filter(
        customer=customer, year=today.year, month=today.month
    ).first()
    available = (
        live_available(project_credit, customer_credit, invoice)
        if invoice
        else max(ZERO, min(project_credit.value, customer_credit.value))
    )
    return plan(
        project_credit,
        customer_credit,
        available,
        invoice,
        today,
        compensate_in,
        period,
    )


def available_now(project_credit) -> decimal.Decimal:
    """What a payment made now could draw, without making one.

    Read-only, so it creates no invoice: with none open this month, nothing has
    been used of it yet.
    """
    customer = project_credit.project.customer
    customer_credit = models.CustomerCredit.objects.filter(customer=customer).first()
    if not customer_credit:
        return ZERO
    today = _current_month()
    invoice = models.Invoice.objects.filter(
        customer=customer, year=today.year, month=today.month
    ).first()
    if invoice is None:
        return max(ZERO, min(project_credit.value, customer_credit.value))
    return live_available(project_credit, customer_credit, invoice)


def open_invoice(customer):
    """The invoice of the current month, where a payment made now goes."""
    today = _current_month()
    invoice, _ = models.Invoice.objects.get_or_create(
        customer=customer, year=today.year, month=today.month
    )
    return invoice


def pay_earlier_invoices(project_credit) -> PaymentPlan | None:
    """Pay now, from what the allocation has left after this month's usage so
    far. None unless the credit is set to pay earlier invoices."""
    customer = project_credit.project.customer
    with transaction.atomic():
        # The organization credit's row is the lock every writer of these
        # balances takes, so two payments, or a payment and a month-end run,
        # cannot both see the same month as unpaid.
        customer_credit = (
            models.CustomerCredit.objects.select_for_update()
            .filter(customer=customer)
            .first()
        )
        if not customer_credit:
            return None
        project_credit = models.ProjectCredit.objects.select_for_update().get(
            pk=project_credit.pk
        )
        if not project_credit.pay_earlier_invoices:
            return None
        invoice = open_invoice(customer)
        available = live_available(project_credit, customer_credit, invoice)
        payment_plan = plan(
            project_credit, customer_credit, available, invoice, _month(invoice)
        )
        return _write(project_credit, customer_credit, payment_plan)


def pay_earlier_invoices_at_month_end(invoice) -> decimal.Decimal:
    """After `invoice`'s own compensation, let every allocation set to do so
    pay earlier months from what it has left. The caller holds the locks."""
    customer_credit = models.CustomerCredit.objects.filter(
        customer=invoice.customer
    ).first()
    if not customer_credit:
        return ZERO
    total = ZERO
    for project_credit in models.ProjectCredit.objects.filter(
        project__customer=invoice.customer, pay_earlier_invoices=True
    ).order_by("pk"):
        available = max(ZERO, min(project_credit.value, customer_credit.value))
        payment_plan = plan(
            project_credit, customer_credit, available, invoice, _month(invoice)
        )
        total += _write(project_credit, customer_credit, payment_plan).total
    return total
