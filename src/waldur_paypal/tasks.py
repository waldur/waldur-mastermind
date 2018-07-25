import logging
from datetime import timedelta, datetime

from celery.task import Task as CeleryTask
from django.conf import settings
from django.utils import timezone

from waldur_core.core import tasks as core_tasks
from waldur_core.structure import SupportedServices

from . import models, executors

logger = logging.getLogger(__name__)


# TODO: Move this mixin to Waldur Core
class ExtensionTaskMixin(CeleryTask):
    """
    This mixin allows to skip task scheduling if extension is disabled.
    Subclasses should implement "is_extension_disabled" method which returns boolean value.
    """
    def is_extension_disabled(self):
        return False

    def apply_async(self, args=None, kwargs=None, **options):
        if self.is_extension_disabled():
            message = 'Task %s is not scheduled, because its extension is disabled.' % self.name
            logger.info(message)
            return self.AsyncResult(options.get('task_id'))
        return super(ExtensionTaskMixin, self).apply_async(args=args, kwargs=kwargs, **options)


class PaypalTaskMixin(ExtensionTaskMixin):
    def is_extension_disabled(self):
        return not settings.WALDUR_PAYPAL['ENABLED']


class DebitCustomers(PaypalTaskMixin, core_tasks.BackgroundTask):
    """ Fetch a list of shared services (services based on shared settings).
        Calculate the amount of consumed resources "yesterday" (make sure this task executed only once a day)
        Reduce customer's balance accordingly
        Stop online resource if needed
    """
    name = 'waldur_paypal.DebitCustomers'

    def is_equal(self, other_task, *args, **kwargs):
        return self.name == other_task.get('name')

    def run(self):
        date = datetime.now() - timedelta(days=1)
        start_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = start_date + timedelta(days=1, microseconds=-1)

        # XXX: it's just a placeholder, it doesn't work properly now nor implemented anyhow
        #      perhaps it should merely use price estimates..

        models = SupportedServices.get_resource_models().values()

        for model in models:
            resources = model.objects.filter(
                service_project_link__service__settings__shared=True)

            for resource in resources:
                try:
                    data = resource.get_cost(start_date, end_date)
                except NotImplementedError:
                    continue
                else:
                    resource.customer.debit_account(data['total_amount'])


class PaymentsCleanUp(PaypalTaskMixin, core_tasks.BackgroundTask):
    name = 'waldur_paypal.PaymentsCleanUp'

    def is_equal(self, other_task, *args, **kwargs):
        return self.name == other_task.get('name')

    def run(self):
        timespan = settings.WALDUR_PAYPAL.get('STALE_PAYMENTS_LIFETIME', timedelta(weeks=1))
        models.Payment.objects.filter(state=models.Payment.States.CREATED, created__lte=timezone.now() - timespan).delete()


class SendInvoices(PaypalTaskMixin, core_tasks.BackgroundTask):
    name = 'waldur_paypal.SendInvoices'

    def is_equal(self, other_task, *args, **kwargs):
        return self.name == other_task.get('name')

    def run(self):
        new_invoices = models.Invoice.objects.filter(backend_id='')

        for invoice in new_invoices.iterator():
            executors.InvoiceCreateExecutor.execute(invoice)
