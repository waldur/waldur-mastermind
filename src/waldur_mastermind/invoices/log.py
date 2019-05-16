from waldur_core.logging.loggers import EventLogger, event_logger


class InvoiceLogger(EventLogger):
    month = int
    year = int
    customer = 'structure.Customer'

    class Meta:
        event_types = ('invoice_created', 'invoice_paid', 'invoice_canceled')
        event_groups = {
            'customers': event_types,
            'invoices': event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context['customer']}


event_logger.register('invoice', InvoiceLogger)
