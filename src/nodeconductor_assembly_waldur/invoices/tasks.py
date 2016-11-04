from datetime import date

from celery import shared_task

from . import models


@shared_task(name='invoices.create_monthly_invoices_for_openstack_packages')
def create_monthly_invoices_for_openstack_packages():
    today = date.today()

    old_invoices = models.Invoice.objects.filter(
        state=models.Invoice.States.PENDING,
        month__lt=today.month,
        year__lte=today.year,
    )
    for invoice in old_invoices:
        invoice.propagate()
