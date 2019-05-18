from waldur_core.logging.loggers import EventLogger, event_logger
from .models import Invoice, Payment


class InvoiceEventLogger(EventLogger):
    invoice = Invoice

    class Meta:
        event_types = ('invoice_deletion_succeeded',
                       'invoice_update_succeeded',
                       'invoice_creation_succeeded')
        event_groups = {'invoices': event_types}

    @staticmethod
    def get_scopes(event_context):
        invoice = event_context['invoice']
        return {invoice.customer}


class PaymentEventLogger(EventLogger):
    payment = Payment

    class Meta:
        event_types = ('payment_creation_succeeded',
                       'payment_approval_succeeded',
                       'payment_cancel_succeeded')
        event_groups = {'payments': event_types}

    @staticmethod
    def get_scopes(event_context):
        payment = event_context['payment']
        return {payment.customer}


event_logger.register('paypal_invoice', InvoiceEventLogger)
event_logger.register('paypal_payment', PaymentEventLogger)
