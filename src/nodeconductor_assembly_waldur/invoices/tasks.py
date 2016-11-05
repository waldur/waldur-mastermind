from datetime import date

from celery import shared_task

from . import models


@shared_task(name='invoices.create_monthly_invoices_for_packages')
def create_monthly_invoices_for_packages():
    """
    This task performs following actions:
        - For every customer change state of the invoices for previous months from "pending" to "billed"
          and freeze their items.
        - Create new invoice for every customer in current month if not created yet.
    """
    today = date.today()

    old_invoices = models.Invoice.objects.filter(
        state=models.Invoice.States.PENDING,
        month__lt=today.month,
        year__lte=today.year,
    )
    for invoice in old_invoices:
        invoice.propagate(month=today.month, year=today.year)
