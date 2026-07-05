"""Thread-local typing context for the CustomerCredit transaction ledger.

Every change of ``CustomerCredit.value`` is recorded as a ``CreditTransaction``
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
import threading

_state = threading.local()


@contextlib.contextmanager
def credit_transaction_type(transaction_type: str, reference=None, comment=""):
    """Type the CustomerCredit value mutations performed inside the block."""
    previous = getattr(_state, "current", None)
    _state.current = (transaction_type, reference, comment)
    try:
        yield
    finally:
        _state.current = previous


def current_credit_transaction_type():
    """Return (transaction_type, reference, comment) declared by the innermost
    ``credit_transaction_type`` block, or (None, None, "") outside any block."""
    return getattr(_state, "current", None) or (None, None, "")
