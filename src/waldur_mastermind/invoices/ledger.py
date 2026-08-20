"""Thread-local typing context for the credit transaction ledger.

Every change of a credit's ``value`` is recorded as a ``CreditTransaction``
row by the ``record_credit_transaction`` post_save handler. Programmatic flows
(compensations, expiry, affiliate fee accrual) declare the semantic type of the
mutation they are about to make by wrapping it in ``credit_transaction_type``;
any mutation made outside such a block is recorded as a staff grant — the only
remaining write path once programmatic flows are typed.

This mirrors the opt-in design of ``audit.py`` (and lives in a separate module
for the same import-graph reason): an untyped path degrades to a conservative
default instead of silently skipping the ledger.
"""

import contextlib
import dataclasses
import decimal
import threading

_state = threading.local()


@dataclasses.dataclass(frozen=True)
class TransactionPart:
    """One semantic component of a single credit value change.

    A month's compensation run draws credit twice for the same balance: once
    against real usage, and once to top the draw up to the minimal-consumption
    floor. Both land in one `value` write, so the ledger cannot tell them apart
    from the delta alone — and they are exactly the two numbers the dashboards
    need separated ("Used" versus "Lost").

    Splitting the write instead (save, then save again) was rejected during
    design review: it fires the policy post_save receivers twice and briefly
    exposes an intermediate balance to policy evaluation. So the writer declares
    the breakdown of the delta it is about to apply, and the ledger handler
    turns it into one row per part.
    """

    transaction_type: str
    amount: decimal.Decimal
    billing_period: object = None


def part_key(instance) -> tuple[str, object]:
    """Identify a credit instance across the context manager boundary."""
    return (instance._meta.label_lower, instance.pk)


@contextlib.contextmanager
def credit_transaction_parts(parts: dict, reference=None, comment=""):
    """Declare the breakdown of the value changes made inside the block.

    `parts` maps `part_key(credit)` to a list of TransactionPart. Credits absent
    from the mapping fall back to the plain `credit_transaction_type` behaviour,
    and so does a breakdown the handler refuses because it does not add up — so
    nest this inside a `credit_transaction_type` block that names the movement,
    or a refusal degrades all the way to an untyped staff grant.
    """
    previous = getattr(_state, "parts", None)
    previous_reference = getattr(_state, "parts_reference", None)
    _state.parts = parts
    _state.parts_reference = (reference, comment)
    try:
        yield
    finally:
        _state.parts = previous
        _state.parts_reference = previous_reference


def current_credit_transaction_parts(instance):
    """(parts, reference, comment) declared for this credit, or (None, None, "")."""
    parts = getattr(_state, "parts", None)
    if not parts:
        return None, None, ""
    reference, comment = getattr(_state, "parts_reference", None) or (None, "")
    return parts.get(part_key(instance)), reference, comment


@contextlib.contextmanager
def credit_transaction_type(
    transaction_type: str, reference=None, comment="", billing_period=None
):
    """Type the credit value mutations performed inside the block.

    ``billing_period`` is for movements that belong to a month rather than to
    the moment they were made — a roll-back reverses a particular month's
    drawdown, and has to be counted against that month or the month it undoes
    keeps the drawdown it no longer has.
    """
    previous = getattr(_state, "current", None)
    _state.current = (transaction_type, reference, comment, billing_period)
    try:
        yield
    finally:
        _state.current = previous


def current_credit_transaction_type():
    """Return (transaction_type, reference, comment, billing_period) declared by
    the innermost ``credit_transaction_type`` block, or (None, None, "", None)
    outside any block."""
    return getattr(_state, "current", None) or (None, None, "", None)
