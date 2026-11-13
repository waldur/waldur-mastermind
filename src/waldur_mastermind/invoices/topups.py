"""Recording customer prepayments as paid credit.

Staff type top-ups in, external systems such as an ERP post them through the
API, and payment gateways confirm them. All of them go through ``top_up``, so
the lock, the lots and the ledger row are the same whichever way money arrived.
"""

import decimal

from django.db import IntegrityError, transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType

from . import ledger, models
from .audit import skip_credit_audit


def top_up(
    credit: models.CustomerCredit,
    amount: decimal.Decimal,
    comment: str,
    channel: str,
    external_reference: str = "",
    reference=None,
) -> models.CustomerCredit:
    """Add paid credit, or reverse a top-up with a negative amount.

    A repeated ``external_reference`` on the same credit is a retry: with the
    same amount it changes nothing, with another amount it is refused. Returns
    the credit as it stands afterwards.
    """
    try:
        with transaction.atomic():
            credit = models.CustomerCredit.objects.select_for_update().get(pk=credit.pk)
            # Looked up under the lock: a concurrent retry waits for the first
            # call to commit and then finds its row.
            if _is_retry(credit, amount, external_reference):
                return credit

            if amount < 0 and -amount > min(credit.paid_balance, credit.value):
                raise exceptions.ValidationError(
                    {"amount": _("Reversal would exceed the paid credit balance.")}
                )
            old_value = credit.value
            with (
                skip_credit_audit(),
                ledger.credit_transaction_type(
                    models.CreditTransaction.Types.TOP_UP,
                    reference=reference,
                    comment=comment,
                    channel=channel,
                    external_reference=external_reference,
                ),
            ):
                credit.value += amount
                credit.save(update_fields=["value"])
            event_logger.emit(
                "{customer_name} credit has been topped up by {amount} from "
                "{old_value} to {new_value}: {comment}"
                if amount > 0
                else "{customer_name} credit top-up has been reversed by {amount} "
                "from {old_value} to {new_value}: {comment}",
                event_type=EventType.CREDIT_TOPPED_UP,
                event_context={
                    "customer": credit.customer,
                    "amount": abs(amount),
                    "old_value": old_value,
                    "new_value": credit.value,
                    "comment": comment,
                    "channel": channel,
                    "external_reference": external_reference,
                },
                scopes=[credit.customer],
            )
    except IntegrityError:
        # Only a writer that skips the lock can record the reference between
        # the lookup and the insert; answer as the lookup would have.
        if not _is_retry(credit, amount, external_reference):
            raise
        credit.refresh_from_db()
    return credit


def _is_retry(credit, amount, external_reference) -> bool:
    """Whether the call repeats a top-up recorded under the same reference.

    Raises a validation error when the repeat carries another amount.
    """
    if not external_reference:
        return False
    recorded = credit.transactions.filter(external_reference=external_reference).first()
    if not recorded:
        return False
    if recorded.amount != amount:
        raise exceptions.ValidationError(
            {
                "external_reference": _(
                    "This reference was already recorded with amount %(amount)s."
                )
                % {"amount": recorded.amount}
            }
        )
    return True
