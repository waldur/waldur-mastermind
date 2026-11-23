"""
Run an Arrow billing action and report what it changes on invoices.

A dry run executes the real task inside a transaction, records every invoice
item it creates, changes or deletes, and rolls back. Applying runs the same
code and commits, so the invoice items in a preview are the ones applying
writes. Work queued for after the commit (component usage billing, usage
limit checks) is not part of the report; Arrow usage is priced at zero, so it
adds no invoice amounts.

Runs take as long as the Arrow calls they make, so the API starts them as a
Celery job and the caller polls for the report (start_job / get_job).
Job state is a database row, which every API and worker process sees.
"""

import logging
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save, pre_delete, pre_save
from django.utils import timezone

from waldur_core.core.middleware import skip_side_effects
from waldur_mastermind.invoices import models as invoices_models

from . import models, tasks

ARROW_LOGGER = "waldur_mastermind.waldur_arrow"

SYNC_BILLING = "sync_billing"
SYNC_CONSUMPTION = "sync_consumption"
SETTLE_MONTH = "settle_month"
ACTIONS = (SYNC_BILLING, SYNC_CONSUMPTION, SETTLE_MONTH)


class _Rollback(Exception):
    pass


def _item_snapshot(item) -> dict:
    return {
        "uuid": item.uuid.hex,
        "name": item.name,
        "amount": item.price,
        "resource_name": item.resource.name if item.resource_id else "",
    }


@dataclass
class InvoiceChangeRecorder:
    """Collects invoice-item changes made while it is connected."""

    before: dict = field(default_factory=dict)  # item pk -> snapshot
    created: set = field(default_factory=set)
    deleted: dict = field(default_factory=dict)  # item pk -> snapshot
    item_invoice: dict = field(default_factory=dict)  # item pk -> invoice pk
    invoice_totals_before: dict = field(default_factory=dict)
    thread: int | None = None

    def _ours(self) -> bool:
        # Receivers are process-wide; record only this run's thread.
        return threading.get_ident() == self.thread

    def _note_invoice(self, invoice_id):
        if invoice_id and invoice_id not in self.invoice_totals_before:
            invoice = invoices_models.Invoice.objects.get(pk=invoice_id)
            self.invoice_totals_before[invoice_id] = invoice.total

    def _pre_save(self, sender, instance, **kwargs):
        if not self._ours():
            return
        self._note_invoice(instance.invoice_id)
        if instance.pk and instance.pk not in self.before:
            current = sender.objects.filter(pk=instance.pk).first()
            if current is not None:
                self.before[instance.pk] = _item_snapshot(current)

    def _post_save(self, sender, instance, created, **kwargs):
        if not self._ours():
            return
        if created:
            self.created.add(instance.pk)
        self.item_invoice[instance.pk] = instance.invoice_id

    def _pre_delete(self, sender, instance, **kwargs):
        if not self._ours():
            return
        self._note_invoice(instance.invoice_id)
        if instance.pk in self.created:
            # Created and removed within the action: no net change.
            self.created.discard(instance.pk)
            self.item_invoice.pop(instance.pk, None)
            return
        self.deleted[instance.pk] = self.before.get(instance.pk) or _item_snapshot(
            instance
        )
        self.item_invoice[instance.pk] = instance.invoice_id

    def __enter__(self):
        uid = f"arrow-billing-changes-{id(self)}"
        model = invoices_models.InvoiceItem
        self.thread = threading.get_ident()
        pre_save.connect(self._pre_save, sender=model, weak=False, dispatch_uid=uid)
        post_save.connect(self._post_save, sender=model, weak=False, dispatch_uid=uid)
        pre_delete.connect(self._pre_delete, sender=model, weak=False, dispatch_uid=uid)
        self._uid = uid
        return self

    def __exit__(self, *exc):
        model = invoices_models.InvoiceItem
        pre_save.disconnect(sender=model, dispatch_uid=self._uid)
        post_save.disconnect(sender=model, dispatch_uid=self._uid)
        pre_delete.disconnect(sender=model, dispatch_uid=self._uid)
        return False

    def report(self) -> list[dict]:
        """The changes grouped by invoice; must run before any rollback."""
        by_invoice: dict = {}
        for pk, invoice_id in self.item_invoice.items():
            change = self._item_change(pk)
            if change:
                by_invoice.setdefault(invoice_id, []).append(change)

        invoices = invoices_models.Invoice.objects.filter(
            pk__in=by_invoice
        ).select_related("customer")
        report = []
        for invoice in invoices:
            report.append(
                {
                    "invoice_uuid": invoice.uuid.hex,
                    "customer_uuid": invoice.customer.uuid.hex,
                    "customer_name": invoice.customer.name,
                    "year": invoice.year,
                    "month": invoice.month,
                    "state": invoice.state,
                    "total_before": self.invoice_totals_before.get(
                        invoice.pk, invoice.total
                    ),
                    "total_after": invoice.total,
                    "items": sorted(by_invoice[invoice.pk], key=lambda c: c["name"]),
                }
            )
        return sorted(report, key=lambda r: (r["customer_name"], r["year"], r["month"]))

    def _item_change(self, pk):
        if pk in self.deleted and not (
            # A savepoint may have rolled the delete back.
            invoices_models.InvoiceItem.objects.filter(pk=pk).exists()
        ):
            old = self.deleted[pk]
            return {
                **old,
                "change": "deleted",
                "amount_before": old["amount"],
                "amount_after": Decimal("0"),
            }
        item = (
            invoices_models.InvoiceItem.objects.filter(pk=pk)
            .select_related("resource")
            .first()
        )
        if item is None:
            return None
        now = _item_snapshot(item)
        if pk in self.created:
            return {
                **now,
                "change": "created",
                "amount_before": Decimal("0"),
                "amount_after": now["amount"],
            }
        old = self.before.get(pk)
        if old is None or (
            old["amount"] == now["amount"] and old["name"] == now["name"]
        ):
            return None
        return {
            **now,
            "change": "updated",
            "amount_before": old["amount"],
            "amount_after": now["amount"],
        }


def _run_action(action: str, year: int, month: int, resource_uuid: str):
    if action == SYNC_BILLING:
        return tasks.sync_arrow_billing(
            year=year, month=month, resource_uuid=resource_uuid
        )
    if action == SYNC_CONSUMPTION:
        return tasks.sync_arrow_consumption(
            year=year, month=month, resource_uuid=resource_uuid
        )
    if action == SETTLE_MONTH:
        return {
            "consumption": tasks.check_and_reconcile_billing(year=year, month=month),
            "billing_lines": tasks.reconcile_arrow_billing(
                year=year, month=month, force=True
            ),
        }
    raise ValueError(f"Unknown action {action}")


class ProblemCollector(logging.Handler):
    """Warnings and errors the Arrow tasks log, which they otherwise swallow."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.thread = threading.get_ident()
        self.problems: list[str] = []

    def emit(self, record):
        if record.thread == self.thread:
            self.problems.append(record.getMessage())

    def __enter__(self):
        logging.getLogger(ARROW_LOGGER).addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger(ARROW_LOGGER).removeHandler(self)
        return False


def run(action: str, year: int, month: int, resource_uuid: str = "", dry_run=True):
    """Run the action; with dry_run nothing it does is kept."""
    taken_at = timezone.now()
    outcome = {}
    with ProblemCollector() as problems:
        try:
            with (
                skip_side_effects() if dry_run else nullcontext(),
                InvoiceChangeRecorder() as recorder,
                transaction.atomic(),
            ):
                result = _run_action(action, year, month, resource_uuid)
                outcome = {"result": result, "invoices": recorder.report()}
                if dry_run:
                    raise _Rollback
        except _Rollback:
            pass
    return {
        "action": action,
        "year": year,
        "month": month,
        "resource_uuid": resource_uuid or None,
        "dry_run": dry_run,
        "taken_at": taken_at,
        "problems": problems.problems,
        **outcome,
    }


# A job still "running" after Celery's hard time limit was killed with its
# worker and will never report.
STALE_AFTER = timedelta(seconds=getattr(settings, "CELERY_TASK_TIME_LIMIT", 1800) + 300)


class JobAlreadyRunning(Exception):
    pass


def _job_state(job: models.ArrowBillingChangesJob) -> dict:
    state, error = job.state, job.error or None
    if (
        state == models.ArrowBillingChangesJob.States.RUNNING
        and job.created < timezone.now() - STALE_AFTER
    ):
        state = models.ArrowBillingChangesJob.States.FAILED
        error = "The job did not finish; its worker stopped. Start it again."
    return {
        "job_id": job.uuid.hex,
        "state": state,
        "report": job.report,
        "error": error,
    }


def get_job(job_id: str):
    job = models.ArrowBillingChangesJob.objects.filter(uuid=job_id).first()
    return _job_state(job) if job else None


def finish_job(job_id: str, report=None, error: str = ""):
    models.ArrowBillingChangesJob.objects.filter(uuid=job_id).update(
        state=models.ArrowBillingChangesJob.States.FAILED
        if error
        else models.ArrowBillingChangesJob.States.DONE,
        report=report,
        error=error,
        modified=timezone.now(),
    )


def start_job(**params) -> dict:
    """Queue the run once the request commits; the caller polls get_job."""
    # One at a time: a run holds invoice and record locks while it calls
    # Arrow, and a second would wait on, or lock out, the first.
    running = models.ArrowBillingChangesJob.objects.select_for_update().filter(
        state=models.ArrowBillingChangesJob.States.RUNNING,
        created__gte=timezone.now() - STALE_AFTER,
    )
    if running.exists():
        raise JobAlreadyRunning
    job = models.ArrowBillingChangesJob.objects.create(**params)
    job_id = job.uuid.hex

    def enqueue():
        try:
            tasks.run_arrow_billing_changes.delay(job_id=job_id, **params)
        except Exception as e:
            finish_job(job_id, error=f"The job could not be queued: {e}")

    transaction.on_commit(enqueue)
    return _job_state(job)
