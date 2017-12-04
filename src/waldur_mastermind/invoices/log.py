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


event_logger.register('invoice', InvoiceLogger)
