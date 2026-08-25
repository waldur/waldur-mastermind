"""Reconstruct credit drawdown that predates the ledger.

`CreditTransaction` only sees movements made after it was wired up. History can
be partly recovered, and it is worth being precise about which parts:

* **Usage draws** are recoverable: every one wrote a negative `InvoiceItem`
  carrying the amount, the project and the billing month. One caveat — when a
  balance is exhausted mid-month the item is written net of tax
  (`compensations.py`, `credit_compensation = value / (1 + tax)`) while the
  balance gave up the full value, so the last draw of an exhausted credit is
  understated by its tax share.
* **Minimal-consumption draws** are exact in amount but not in month. They
  never wrote an invoice item; their only trace is the audit event
  `reduction_of_{customer,project}_credit_due_to_minimal_consumption`, whose
  `consumption` is a full Decimal. The event carries no billing period, so the
  month is inferred from when it was emitted, read in the deployment's own
  timezone because that is the one finalization is scheduled in — right for
  scheduled finalization, wrong for a manual re-run or a seeded history.
* **The granted amount is not recoverable.** Every event that moves a balance
  in bulk — grants, staff edits, expiry, rollback, termination — truncates both
  values to whole units, so chaining them accumulates error. What the evidence
  cannot explain is written as one labelled opening-balance row, keeping
  `granted = used + lost + remaining` true by construction while marking the
  part that is a plug rather than a record.

Audit events are prunable (`waldur_core.logging` runs retention-based cleanup),
so coverage varies per deployment and shrinks with age. Run --dry-run first: it
reports what evidence exists before anything is written.
"""

import collections
import datetime
import decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Min, Sum
from django.utils import timezone

from waldur_core.core.utils import chunked_queryset
from waldur_core.logging import models as logging_models
from waldur_mastermind.invoices import models

BACKFILL_PREFIX = "backfill:"
USAGE_COMMENT = f"{BACKFILL_PREFIX} reconstructed from the compensation invoice items"
FLOOR_COMMENT = f"{BACKFILL_PREFIX} reconstructed from the audit event"
FLOOR_INFERRED_COMMENT = (
    f"{FLOOR_COMMENT}; billing period inferred from the event timestamp"
)
OPENING_COMMENT = (
    f"{BACKFILL_PREFIX} opening balance — the grants behind it predate the ledger"
)
UNEXPLAINED_COMMENT = (
    f"{BACKFILL_PREFIX} drawdown with no surviving trace — no invoice item and "
    "no audit event accounts for it"
)

CUSTOMER_FLOOR_EVENT = "reduction_of_customer_credit_due_to_minimal_consumption"
PROJECT_FLOOR_EVENT = "reduction_of_project_credit_due_to_minimal_consumption"

ZERO = decimal.Decimal(0)


def previous_month(value: datetime.date) -> datetime.date:
    return (value.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)


class Command(BaseCommand):
    help = (
        "Reconstruct credit drawdown that predates the transaction ledger, from "
        "compensation invoice items and minimal-consumption audit events. Run "
        "with --dry-run first to see how much evidence survives."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written, and write nothing.",
        )
        parser.add_argument(
            "--customer",
            dest="customer_uuid",
            help="Only the credits of the organization with this UUID.",
        )
        parser.add_argument(
            "--project",
            dest="project_uuid",
            help=(
                "Only the allocation of the project with this UUID. The "
                "organization credit is a separate balance and is left alone."
            ),
        )
        parser.add_argument(
            "--since",
            help="Ignore evidence before this billing month (YYYY-MM).",
        )
        parser.add_argument(
            "--until",
            help="Ignore evidence after this billing month (YYYY-MM).",
        )
        parser.add_argument(
            "--infer-period",
            choices=["previous-month", "none"],
            default="previous-month",
            help=(
                "How to date minimal-consumption draws, whose events carry no "
                "billing period. 'previous-month' assumes the run billed the "
                "month before it was emitted, which holds for scheduled "
                "finalization; 'none' leaves the period empty, so the rows "
                "count towards the totals but towards no month — and are then "
                "not subject to --since/--until."
            ),
        )
        parser.add_argument(
            "--no-opening-balance",
            action="store_true",
            help=(
                "Skip the balancing row, leaving only rows backed by evidence. "
                "Totals then no longer reconcile to the current value of any "
                "credit granted before the ledger existed."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help=(
                "Redo credits that already carry backfilled rows: the previous "
                "reconstruction is deleted and written again. Only rows this "
                "command wrote are touched."
            ),
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.infer_period = options["infer_period"]
        self.write_opening_balance = not options["no_opening_balance"]
        self.force = options["force"]
        self.since = self._parse_month(options.get("since"), "--since")
        self.until = self._parse_month(options.get("until"), "--until")
        if self.since and self.until and self.since > self.until:
            raise CommandError("--since is later than --until.")

        customer_credits, project_credits = self._select_credits(options)
        if not customer_credits and not project_credits:
            self.stdout.write("No credits match the given filters.")
            return

        summary = collections.Counter()
        for credit in customer_credits:
            self._process(credit, is_project=False, summary=summary)
        for credit in project_credits:
            self._process(credit, is_project=True, summary=summary)

        self._report(summary)

    def _parse_month(self, value, label):
        if not value:
            return None
        try:
            return datetime.datetime.strptime(value, "%Y-%m").date()
        except ValueError:
            raise CommandError(f"Invalid {label} {value!r}: expected YYYY-MM.")

    def _select_credits(self, options):
        customer_credits = models.CustomerCredit.objects.select_related("customer")
        project_credits = models.ProjectCredit.objects.select_related(
            "project", "project__customer"
        )

        if options.get("customer_uuid"):
            customer_credits = customer_credits.filter(
                customer__uuid=options["customer_uuid"]
            )
            project_credits = project_credits.filter(
                project__customer__uuid=options["customer_uuid"]
            )
        if options.get("project_uuid"):
            customer_credits = customer_credits.none()
            project_credits = project_credits.filter(
                project__uuid=options["project_uuid"]
            )

        return list(customer_credits.order_by("id")), list(
            project_credits.order_by("id")
        )

    def _backfilled_rows(self, credit, is_project):
        field = "project_credit" if is_project else "credit"
        return models.CreditTransaction.objects.filter(
            **{field: credit}, comment__startswith=BACKFILL_PREFIX
        )

    def _in_range(self, period):
        if period is None:
            return True
        if self.since and period < self.since:
            return False
        if self.until and period > self.until:
            return False
        return True

    def _usage_draws(self, credit, is_project):
        """One row per billing month, from the compensation items of that month."""
        if is_project:
            # Compensation items name the project they offset; the credit FK on
            # them always points at the organization credit, never at the
            # allocation, so the project is the only handle there is.
            items = models.InvoiceItem.objects.filter(
                project=credit.project, credit__isnull=False
            )
        else:
            items = models.InvoiceItem.objects.filter(credit=credit)

        rows = []
        grouped = (
            items.filter(unit_price__lt=0)
            .values("invoice__year", "invoice__month")
            .annotate(total=Sum("unit_price"))
            .order_by("invoice__year", "invoice__month")
        )
        for entry in grouped:
            period = datetime.date(entry["invoice__year"], entry["invoice__month"], 1)
            if not self._in_range(period) or not entry["total"]:
                continue
            rows.append(
                dict(
                    amount=entry["total"],
                    transaction_type=models.CreditTransaction.Types.COMPENSATION,
                    billing_period=period,
                    comment=USAGE_COMMENT,
                )
            )
        return rows

    def _floor_draws(self, credit, is_project, ledger_start):
        """One row per audit event: the floor draw's only surviving trace."""
        if is_project:
            events = logging_models.Event.objects.filter(
                event_type=PROJECT_FLOOR_EVENT,
                context__project_uuid=credit.project.uuid.hex,
            )
        else:
            events = logging_models.Event.objects.filter(
                event_type=CUSTOMER_FLOOR_EVENT,
                context__customer_uuid=credit.customer.uuid.hex,
            )

        recorded_events = 0
        if ledger_start:
            # From here on the movement has a row of its own.
            recorded_events = events.filter(created__gte=ledger_start).count()
            events = events.filter(created__lt=ledger_start)

        rows = []
        # Client-side chunks, not a server-side cursor: this walk runs in
        # autocommit, so a cursor declared here can be handed a different
        # backend by a transaction-mode pooler before the next fetch. The
        # keyset walk orders by pk; each row is derived from its own event
        # alone, so the created-order this replaces bought nothing.
        for event in chunked_queryset(events):
            try:
                amount = decimal.Decimal(str((event.context or {}).get("consumption")))
            except (decimal.InvalidOperation, TypeError):
                continue
            if not amount:
                continue
            period = None
            if self.infer_period == "previous-month":
                # In the deployment's own timezone, not UTC. Finalization is
                # scheduled at local midnight on the 1st, which east of UTC is
                # stored as the last day of the month before — so reading the
                # stored date directly dates the draw a whole month early.
                period = previous_month(timezone.localdate(event.created))
                if not self._in_range(period):
                    continue
            rows.append(
                dict(
                    amount=-abs(amount),
                    transaction_type=models.CreditTransaction.Types.MINIMAL_DRAW,
                    billing_period=period,
                    comment=FLOOR_INFERRED_COMMENT if period else FLOOR_COMMENT,
                )
            )
        return rows, recorded_events

    def _recorded(self, credit, is_project):
        field = "project_credit" if is_project else "credit"
        return models.CreditTransaction.objects.filter(**{field: credit}).exclude(
            comment__startswith=BACKFILL_PREFIX
        )

    def _covered_periods(self, credit, is_project):
        """Months the ledger already records, and must not be told about twice.

        Backfilling is not an all-or-nothing operation: the ledger starts
        recording on the day it is deployed, so a credit older than that has
        months on both sides of the line. Only the months with no row of their
        own are reconstructed.
        """
        return set(
            self._recorded(credit, is_project)
            .exclude(billing_period__isnull=True)
            .values_list("billing_period", flat=True)
        )

    def _ledger_start(self, credit, is_project):
        """When the ledger began recording this credit, if it ever did.

        The month a reconstructed row belongs to is not enough to tell whether
        the ledger already has it: a floor draw's month is inferred, and an
        inferred month that lands outside the recorded ones would slip past the
        month check and be counted twice. The event's own timestamp is not
        inferred, so it is compared against the moment recording started.
        """
        return self._recorded(credit, is_project).aggregate(first=Min("created"))[
            "first"
        ]

    def _process(self, credit, is_project, summary):
        label = (
            f"project {credit.project.name}"
            if is_project
            else f"organization {credit.customer.name}"
        )
        existing = self._backfilled_rows(credit, is_project)
        if existing.exists() and not self.force:
            self.stdout.write(f"{label}: already backfilled, skipping (--force redoes)")
            summary["skipped"] += 1
            return

        floor_rows, recorded_events = self._floor_draws(
            credit, is_project, self._ledger_start(credit, is_project)
        )
        candidates = self._usage_draws(credit, is_project) + floor_rows
        covered = self._covered_periods(credit, is_project)
        rows = [row for row in candidates if row["billing_period"] not in covered]
        already_recorded = len(candidates) - len(rows) + recorded_events

        totals = collections.defaultdict(decimal.Decimal)
        for row in rows:
            totals[row["transaction_type"]] += row["amount"]
        used = -totals[models.CreditTransaction.Types.COMPENSATION]
        lost = -totals[models.CreditTransaction.Types.MINIMAL_DRAW]
        inferred = sum(
            1
            for row in rows
            if row["comment"] == FLOOR_INFERRED_COMMENT and row["billing_period"]
        )

        # Whatever the evidence and the existing ledger together cannot account
        # for. Its sign says which of two different things it is: a positive
        # remainder is value the credit already held (grants and staff edits,
        # which the audit trail keeps only to the whole unit); a negative one is
        # drawdown that left no trace at all.
        plug = None
        if self.write_opening_balance:
            plug = (
                credit.value
                - (
                    self._recorded(credit, is_project).aggregate(total=Sum("amount"))[
                        "total"
                    ]
                    or ZERO
                )
                - sum(row["amount"] for row in rows)
            ) or None

        self.stdout.write(
            f"{label}: used {used:,.2f}, lost {lost:,.2f} over {len(rows)} row(s)"
            + (
                f", {already_recorded} already in the ledger"
                if already_recorded
                else ""
            )
            + (f", {inferred} with an inferred month" if inferred else "")
            + (
                ", already balanced"
                if plug is None
                else f", opening balance {plug:,.2f}"
                if plug > 0
                else f", {-plug:,.2f} unexplained"
            )
        )
        if plug is not None and plug < 0:
            self.stdout.write(
                self.style.WARNING(
                    f"  {-plug:,.2f} left this balance with nothing to show for it. "
                    "Either the movement predates both the invoice items and the "
                    "audit retention window, or drawdown here is being attributed "
                    f"to the wrong credit — the credit was created "
                    f"{credit.created:%Y-%m-%d}, so check the earliest months "
                    "against that date and exclude them with --since."
                )
            )
            summary["unexplained"] += 1

        summary["credits"] += 1
        summary["rows"] += len(rows)
        summary["already_recorded"] += already_recorded
        summary["inferred"] += inferred
        summary["plugs"] += 1 if plug is not None else 0

        if self.dry_run:
            return

        attribution = (
            {
                "project_credit": credit,
                "project_uuid": credit.project.uuid.hex,
                "project_name": credit.project.name,
            }
            if is_project
            else {"credit": credit}
        )
        with transaction.atomic():
            existing.delete()
            if plug is not None:
                # Written first, so the ledger reads as an opening balance drawn
                # down by the movements that follow it.
                models.CreditTransaction.objects.create(
                    amount=plug,
                    transaction_type=models.CreditTransaction.Types.ADJUSTMENT,
                    comment=OPENING_COMMENT if plug > 0 else UNEXPLAINED_COMMENT,
                    **attribution,
                )
            models.CreditTransaction.objects.bulk_create(
                models.CreditTransaction(**row, **attribution) for row in rows
            )

    def _report(self, summary):
        written = summary["rows"] + summary["plugs"]
        self.stdout.write("")
        self.stdout.write(
            f"{summary['credits']} credit(s) processed, {summary['skipped']} skipped; "
            f"{'would write' if self.dry_run else 'wrote'} {written} row(s), "
            f"of which {summary['plugs']} balancing row(s)."
        )
        if summary["already_recorded"]:
            self.stdout.write(
                f"{summary['already_recorded']} reconstructed row(s) were dropped "
                "because the ledger already records their month."
            )
        if summary["inferred"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{summary['inferred']} minimal-consumption row(s) carry a month "
                    "inferred from the audit event timestamp, which only holds for "
                    "scheduled finalization runs. Pass --infer-period=none to leave "
                    "those months empty instead of guessing them."
                )
            )
        if summary["unexplained"]:
            self.stdout.write(
                self.style.WARNING(
                    f"{summary['unexplained']} credit(s) lost value that no invoice "
                    "item or audit event accounts for — see the warnings above "
                    "before writing."
                )
            )
        if self.dry_run:
            self.stdout.write("Dry run: nothing was written.")
