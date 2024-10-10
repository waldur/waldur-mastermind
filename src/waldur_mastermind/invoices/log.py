import datetime
import decimal

from waldur_core.logging.loggers import EventLogger, event_logger


class InvoiceLogger(EventLogger):
    month = int
    year = int
    customer = "structure.Customer"

    class Meta:
        event_types = (
            "invoice_created",
            "invoice_paid",
            "invoice_canceled",
            "payment_created",
            "payment_removed",
        )
        event_groups = {
            "customers": event_types,
            "invoices": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context["customer"]}


event_logger.register("invoice", InvoiceLogger)


class InvoiceItemLogger(EventLogger):
    customer = "structure.Customer"

    class Meta:
        event_types = (
            "invoice_item_created",
            "invoice_item_updated",
            "invoice_item_deleted",
        )
        event_groups = {
            "customers": event_types,
            "invoices": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context["customer"]}


event_logger.register("invoice_item", InvoiceItemLogger)


class PaymentLogger(EventLogger):
    amount = decimal.Decimal
    customer = "structure.Customer"

    class Meta:
        event_types = (
            "payment_added",
            "payment_removed",
        )
        event_groups = {
            "customers": event_types,
        }

    @staticmethod
    def get_scopes(event_context):
        return {event_context["customer"]}


event_logger.register("payment", PaymentLogger)


class CreditLogger(EventLogger):
    consumption = decimal.Decimal
    minimal_consumption = decimal.Decimal
    customer = "structure.Customer"
    invoice_item = str
    credit_end_date = datetime.date

    class Meta:
        event_types = (
            "reduction_of_credit_due_to_minimal_consumption",
            "reduction_of_credit",
            "set_to_zero_overdue_credit",
        )
        event_groups = {
            "customers": event_types,
            "invoices": event_types,
        }
        nullable_fields = [
            "consumption",
            "minimal_consumption",
            "invoice_item",
            "credit_end_date",
        ]

    @staticmethod
    def get_scopes(event_context):
        return {event_context["customer"]}


event_logger.register("credit", CreditLogger)
