import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from waldur_core.core import tasks as core_tasks
from waldur_core.core.tasks import ExtensionTaskMixin

from . import executors, models

logger = logging.getLogger(__name__)


class PaypalTaskMixin(ExtensionTaskMixin):
    def is_extension_disabled(self):
        return not settings.WALDUR_PAYPAL["ENABLED"]


class PaymentsCleanUp(PaypalTaskMixin, core_tasks.BackgroundTask):
    name = "waldur_paypal.PaymentsCleanUp"

    def is_equal(self, other_task, *args, **kwargs):
        return self.name == other_task.get("name")

    def run(self):
        timespan = settings.WALDUR_PAYPAL.get(
            "STALE_PAYMENTS_LIFETIME", timedelta(weeks=1)
        )
        models.Payment.objects.filter(
            state=models.Payment.States.CREATED, created__lte=timezone.now() - timespan
        ).delete()


class SendInvoices(PaypalTaskMixin, core_tasks.BackgroundTask):
    name = "waldur_paypal.SendInvoices"

    def is_equal(self, other_task, *args, **kwargs):
        return self.name == other_task.get("name")

    def run(self):
        new_invoices = models.Invoice.objects.filter(backend_id="")

        for invoice in new_invoices.iterator():
            executors.InvoiceCreateExecutor.execute(invoice)
