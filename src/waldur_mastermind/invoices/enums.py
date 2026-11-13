class CompensateIn:
    """Which invoice a project credit's payment of an earlier invoice goes on.

    Here rather than on ProjectCredit so that the OpenAPI settings can name the
    choice set without importing models.
    """

    # On the invoice open when the payment is made: earlier invoices stay as
    # they were issued.
    CURRENT_MONTH = "current_month"
    # On the paid month's own invoice, which changes the total of an invoice
    # already issued, and possibly paid.
    INCURRED_MONTH = "incurred_month"

    CHOICES = (
        (CURRENT_MONTH, "Current month"),
        (INCURRED_MONTH, "Month the cost was incurred"),
    )


class CreditTransactionChannel:
    """How a top-up reached the credit ledger.

    Here rather than on CreditTransaction so that the OpenAPI settings can name
    the choice set without importing models.
    """

    # Typed in by staff, and every movement that is not a top-up.
    MANUAL = "manual"
    # Posted by an external system, such as an ERP, with its own reference.
    API = "api"
    # Confirmed by a payment gateway.
    GATEWAY = "gateway"

    CHOICES = (
        (MANUAL, "Manual"),
        (API, "API"),
        (GATEWAY, "Payment gateway"),
    )
