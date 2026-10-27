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
