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
    old_value = int
    new_value = int
    old_offerings = set
    new_offerings = set
    customer = "structure.Customer"
    project = "structure.Project"
    invoice_item = str
    credit_end_date = datetime.date

    class Meta:
        event_types = (
            "reduction_of_credit_due_to_minimal_consumption",
            "reduction_of_credit",
            "set_to_zero_overdue_credit",
            "update_of_credit_by_staff",
            "create_of_credit_by_staff",
            "roll_back_customer_credit",
            "roll_back_project_credit",
            "allowed_offerings_have_been_updated",
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
            "old_value",
            "new_value",
            "project",
            "old_offerings",
            "new_offerings",
        ]

    @staticmethod
    def get_scopes(event_context):
        return {event_context["customer"]}


event_logger.register("credit", CreditLogger)


def log_roll_back_customer_credit(customer, old_value, new_value):
    event_logger.credit.info(
        "Customer credit for {{ customer }} has been rolled back from {{ old_value }} to {{ new_value }}.",
        event_type="roll_back_customer_credit",
        event_context={
            "old_value": int(old_value),
            "new_value": int(new_value),
            "customer": customer,
        },
    )


def log_roll_back_project_credit(customer, project, old_value, new_value):
    event_logger.credit.info(
        "Project credit for {{ project }} has been rolled back from {{ old_value }} to {{ new_value }}.",
        event_type="roll_back_project_credit",
        event_context={
            "old_value": int(old_value),
            "new_value": int(new_value),
            "customer": customer,
            "project": project,
        },
    )


def log_changing_of_offerings(customer, old_offerings, new_offerings):
    event_logger.credit.info(
        "Allowed offerings of {{ customer }} have been updated from {{ old_offerings }} to {{ new_offerings }}.",
        event_type="allowed_offerings_have_been_updated",
        event_context={
            "old_offerings": old_offerings,
            "new_offerings": new_offerings,
            "customer": customer,
        },
    )
