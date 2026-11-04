"""Credit lots: what an organization credit is made of.

An organization credit is one balance, but the money in it comes from
different places: staff grants (granted credit), customer prepayments recorded by
staff (paid), and what the organization earned (affiliate fees, transfers).
They are not interchangeable — an affiliate earns a fee on invoices paid from
a prepayment but not on those paid from a grant, and only earnings may leave
the platform — so each inflow opens a lot of its kind and each outflow draws
lots down. `CustomerCredit.value` stays the sum of what the lots have left, so
everything that reads the balance (cost policies, project allocations) keeps
reading one number.

Lots are kept by the ledger handler: every value change is already recorded
there as a `CreditTransaction`, and `apply_transaction` turns that row into lot
movements. Project allocations are caps on the organization balance rather
than money of their own, so they have no lots.
"""

import decimal
import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, Sum

from waldur_mastermind.common.utils import quantize_price

from . import models

logger = logging.getLogger(__name__)

Types = models.CreditTransaction.Types
Kinds = models.CreditLot.Kinds

ZERO = decimal.Decimal("0")

# Inflows of anything other than these open a granted lot: an untyped
# write is a staff grant.
INFLOW_KINDS = {
    Types.TOP_UP: Kinds.PAID,
    Types.RECLASSIFICATION: Kinds.PAID,
    Types.AFFILIATE_FEE: Kinds.EARNED,
    Types.TRANSFER_IN: Kinds.EARNED,
    Types.WITHDRAWABLE_ADJUSTMENT: Kinds.EARNED,
}

# Outflows that take money of one kind first: earnings leaving the platform
# draw on earnings, and a negative top-up reverses a prepayment. Callers check
# that the kind covers the amount; the rest of the lots only cover a shortfall,
# so that the lots keep adding up to the value.
OUTFLOW_KINDS = {
    Types.PAYOUT: (Kinds.EARNED,),
    Types.TRANSFER_OUT: (Kinds.EARNED,),
    Types.WITHDRAWABLE_ADJUSTMENT: (Kinds.EARNED,),
    Types.TOP_UP: (Kinds.PAID,),
    Types.RECLASSIFICATION: (Kinds.GRANTED,),
}

# Movements made on behalf of an invoice: a roll-back or a correction that
# gives credit back returns it to the lots these took it from. Payments of
# earlier invoices are not among them: they are never rolled back.
INVOICE_TYPES = (
    Types.COMPENSATION,
    Types.MINIMAL_DRAW,
    Types.ROLLBACK,
    Types.ADJUSTMENT,
)
RESTORING_TYPES = (Types.ROLLBACK, Types.ADJUSTMENT)


def apply_transaction(row: models.CreditTransaction):
    """Move the lots of an organization credit by one ledger row."""
    shortfall = _apply(row)
    if shortfall:
        # Only possible when the lots have drifted from the value, e.g.
        # after a queryset update() that bypassed the ledger.
        logger.warning(
            "Credit lots of %s are short by %s for ledger row %s.",
            row.credit,
            shortfall,
            row.uuid,
        )


def _apply(row) -> decimal.Decimal:
    """Move the lots by one row; return what an outflow found no lots for."""
    if not row.credit_id or not row.amount:
        return ZERO

    with transaction.atomic():
        if row.amount > 0:
            leftover = row.amount
            if row.transaction_type in RESTORING_TYPES and row.object_id:
                leftover = _restore(row, leftover)
            if leftover > 0:
                kind = INFLOW_KINDS.get(row.transaction_type, Kinds.GRANTED)
                _open(row, kind, leftover)
            return ZERO

        amount = -row.amount
        if row.transaction_type in OUTFLOW_KINDS:
            amount = _draw(row, amount, OUTFLOW_KINDS[row.transaction_type])
        return _draw(row, amount, Kinds.DRAW_ORDER)


def _open(row, kind, amount):
    models.CreditLot.objects.create(
        credit_id=row.credit_id,
        kind=kind,
        amount=amount,
        remaining=amount,
        source=row,
    )


def _draw(row, amount, kinds) -> decimal.Decimal:
    """Take `amount` from the lots of `kinds`; return what they could not cover."""
    if amount <= 0:
        return ZERO
    lots = sorted(
        models.CreditLot.objects.select_for_update().filter(
            credit_id=row.credit_id, kind__in=kinds, remaining__gt=0
        ),
        key=lambda lot: (kinds.index(lot.kind), lot.created, lot.id),
    )
    draws = []
    for lot in lots:
        if amount <= 0:
            break
        taken = min(lot.remaining, amount)
        lot.remaining -= taken
        lot.save(update_fields=["remaining"])
        draws.append(models.CreditLotDraw(lot=lot, transaction=row, amount=taken))
        amount -= taken
    models.CreditLotDraw.objects.bulk_create(draws)
    return amount


def _restore(row, amount) -> decimal.Decimal:
    """Give `amount` back to the lots that earlier movements for the same
    invoice took it from, the last drawn first; return what is left over."""
    outstanding = dict(
        models.CreditLotDraw.objects.filter(
            transaction__credit_id=row.credit_id,
            transaction__content_type_id=row.content_type_id,
            transaction__object_id=row.object_id,
            transaction__transaction_type__in=INVOICE_TYPES,
        )
        .values_list("lot")
        .annotate(total=Sum("amount"))
        .filter(total__gt=0)
    )
    lots = sorted(
        models.CreditLot.objects.select_for_update().filter(pk__in=outstanding),
        key=lambda lot: (Kinds.DRAW_ORDER.index(lot.kind), lot.created, lot.id),
        reverse=True,
    )
    draws = []
    for lot in lots:
        if amount <= 0:
            break
        given = min(outstanding[lot.pk], amount)
        lot.remaining += given
        lot.save(update_fields=["remaining"])
        draws.append(models.CreditLotDraw(lot=lot, transaction=row, amount=-given))
        amount -= given
    models.CreditLotDraw.objects.bulk_create(draws)
    return amount


def resplit(credit: models.CustomerCredit):
    """Rebuild the lots of a credit whose value was written past the ledger
    handler, as a bulk import does.

    The ledger is replayed oldest row first, as the handler would have moved
    the lots, so what was spent of earnings or prepayments stays spent and
    roll-backs find the draws they return. Whatever the ledger does not
    explain — a value written with no row, or rows missing — is settled
    last: a surplus is granted credit, a deficit is drawn in the usual order.
    """
    with transaction.atomic():
        credit = models.CustomerCredit.objects.select_for_update().get(pk=credit.pk)
        credit.lots.all().delete()
        for row in credit.transactions.order_by("created", "id"):
            _apply(row)

        held = credit.lots.aggregate(sum=Sum("remaining"))["sum"] or ZERO
        if credit.value != held:
            # Moved with no ledger row, so this log is its only record.
            logger.warning(
                "Credit lots of %s rebuilt from the ledger hold %s against a "
                "value of %s; settling the difference.",
                credit,
                held,
                credit.value,
            )
        if credit.value > held:
            models.CreditLot.objects.create(
                credit=credit,
                kind=Kinds.GRANTED,
                amount=credit.value - held,
                remaining=credit.value - held,
            )
            return
        excess = held - credit.value
        for lot in sorted(
            credit.lots.filter(remaining__gt=0),
            key=lambda lot: (Kinds.DRAW_ORDER.index(lot.kind), lot.created, lot.id),
        ):
            if excess <= 0:
                break
            taken = min(lot.remaining, excess)
            lot.remaining -= taken
            lot.save(update_fields=["remaining"])
            excess -= taken


def reclassify(credit: models.CustomerCredit, amount, comment):
    """Mark `amount` of granted credit as paid, leaving the value alone.

    The ledger records it as a pair of rows that cancel out, so the trace of
    the credit still adds up to its value. The caller holds the credit row
    lock and has checked that the granted balance covers the amount.
    """
    with transaction.atomic():
        out = models.CreditTransaction.objects.create(
            credit=credit,
            amount=-amount,
            transaction_type=Types.RECLASSIFICATION,
            comment=comment,
        )
        shortfall = _draw(out, amount, (Kinds.GRANTED,))
        if shortfall:
            raise ValueError(f"Granted credit of {credit} is short by {shortfall}.")
        into = models.CreditTransaction.objects.create(
            credit=credit,
            amount=amount,
            transaction_type=Types.RECLASSIFICATION,
            comment=comment,
        )
        _open(into, Kinds.PAID, amount)


def paid_compensation(invoice: models.Invoice) -> decimal.Decimal:
    """The part of the credit compensation written on the invoice that paid
    lots covered.

    Net of tax, like the invoice price it is added to. Compensation is
    re-applied as clear-then-apply, so the latest compensation row for the
    invoice is the one its month's compensation items belong to. Payments of
    earlier invoices written on it (see `settlement`) count too: they lower
    its price as well, and the month they pay already earned its fee.
    """
    credit = models.CustomerCredit.objects.filter(customer=invoice.customer).first()
    if not credit:
        return ZERO
    rows = credit.transactions.filter(
        content_type=ContentType.objects.get_for_model(models.Invoice),
        object_id=invoice.pk,
    )
    return _paid_month_compensation(credit, invoice, rows) + _paid_draw(
        rows.filter(transaction_type=Types.HISTORICAL_COMPENSATION, amount__lt=0)
    )


def granted_drawn(rows) -> decimal.Decimal:
    """What `rows` took from granted lots, net of what they gave back."""
    return _draws_by_kind(rows)[Kinds.GRANTED]


def paid_drawn(rows) -> decimal.Decimal:
    """What `rows` took from paid lots, net of what they gave back."""
    return _paid_draw(rows)


def _paid_draw(rows) -> decimal.Decimal:
    return _draws_by_kind(rows)[Kinds.PAID]


def _draws_by_kind(rows) -> dict:
    draws = dict.fromkeys(Kinds.DRAW_ORDER, ZERO)
    draws.update(
        models.CreditLotDraw.objects.filter(transaction__in=rows)
        .values_list("lot__kind")
        .annotate(sum=Sum("amount"))
    )
    return draws


def _paid_month_compensation(credit, invoice, rows) -> decimal.Decimal:
    row = (
        rows.filter(transaction_type=Types.COMPENSATION)
        .order_by("-created", "-id")
        .first()
    )
    if not row or row.amount >= 0:
        return ZERO
    draws = _draws_by_kind([row])
    if not draws[Kinds.PAID]:
        return ZERO
    compensated = -(
        invoice.items.filter(credit=credit, compensated_period__isnull=True).aggregate(
            sum=Sum(F("quantity") * F("unit_price"))
        )["sum"]
        or ZERO
    )
    return quantize_price(
        min(compensated, net_paid(draws, -row.amount, compensated, invoice))
    )


def net_paid(draws, drawn, compensated, invoice) -> decimal.Decimal:
    """The paid draw of a compensation run, net of tax.

    Every draw pays its item's net price, except the last one of a credit
    that runs out: it gives up all that is left, `gross`, for an item
    compensated by gross / (1 + tax). That draw is the tail of the run in
    draw order, and each lot in it covers net in proportion to what it gave.
    """
    paid = draws[Kinds.PAID]
    tax = decimal.Decimal(invoice.tax_percent or 0) / 100
    reduction = drawn - compensated
    if reduction <= 0 or not tax:
        return paid
    net_share = 1 / (1 + tax)
    gross = min(drawn, reduction / (1 - net_share))
    paid_start = draws[Kinds.GRANTED]
    paid_before_tail = max(ZERO, min(paid_start + paid, drawn - gross) - paid_start)
    return paid_before_tail + (paid - paid_before_tail) * net_share
