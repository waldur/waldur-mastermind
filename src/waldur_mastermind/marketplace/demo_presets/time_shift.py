"""Move a preset's billing history onto the month it is loaded in.

``scripts/generate_realistic_credit_preset.py`` anchors its months on
``date.today()`` and writes them absolutely, so a committed preset is only
accurate on the day it was generated. Months later, questions about "last
month" reach a month the preset holds no data for -- and the assistant that
answers "nothing was billed" correctly is scored as having failed.

Shifting on load keeps the shape the generator intended (a run of
consecutive months ending in the current one) without regenerating a
committed JSON file every month.

A preset opts in with ``"_metadata": {"rebase_billing_history": true}``.
The loader is shared by every preset, and the ones that never asked for
this carry history the shift does not reach (credit ledger rows, policy
firing times, order completions) -- moving their invoices alone would
leave that history pointing at the wrong months.
"""

import calendar
import copy
from datetime import date, datetime

from django.utils import timezone

# Fields holding a point in the billing history, per entity collection.
# Deliberately absent: ``resources.created``, and the credits' ``created`` /
# ``end_date`` -- an expiry is not history, and moving it by the same offset
# would push a grant's end somewhere the generator never meant.
_HISTORY_FIELDS = {
    "invoices": ("created", "invoice_date"),
    "invoice_items": ("start", "end"),
    "component_usages": ("date", "billing_period"),
}


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    index = (year * 12 + month - 1) + offset
    return divmod(index, 12)[0], divmod(index, 12)[1] + 1


def _shift_timestamp(value: str, offset: int) -> str:
    """Shift an ISO date or datetime by whole months, keeping the time.

    The day is clamped to the target month, so an item ending on the 31st
    lands on the 28th of a February rather than overflowing into March.
    """
    parsed = datetime.fromisoformat(value)
    year, month = _shift_month(parsed.year, parsed.month, offset)
    day = min(parsed.day, calendar.monthrange(year, month)[1])
    shifted = parsed.replace(year=year, month=month, day=day)
    # Round-trip in the format it arrived in: dates stay dates.
    return shifted.date().isoformat() if len(value) == 10 else shifted.isoformat()


def rebase_to_current_month(data: dict, today: date | None = None) -> dict:
    """Return a copy of ``data`` whose newest invoice month is ``today``'s.

    Only a preset that declares ``_metadata.rebase_billing_history`` is
    touched. One with no invoices is returned unchanged, as is one already
    anchored on the right month.
    """
    if not (data.get("_metadata") or {}).get("rebase_billing_history"):
        return data
    invoices = data.get("invoices") or []
    if not invoices:
        return data

    today = today or timezone.localdate()
    newest_year, newest_month = max((i["year"], i["month"]) for i in invoices)
    offset = (today.year - newest_year) * 12 + (today.month - newest_month)
    if offset == 0:
        return data

    shifted = copy.deepcopy(data)

    for invoice in shifted["invoices"]:
        invoice["year"], invoice["month"] = _shift_month(
            invoice["year"], invoice["month"], offset
        )

    for collection, fields in _HISTORY_FIELDS.items():
        for row in shifted.get(collection) or []:
            for field in fields:
                if row.get(field):
                    row[field] = _shift_timestamp(row[field], offset)

    return shifted
