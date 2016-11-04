from __future__ import unicode_literals

from datetime import date

from . import models


def add_new_openstack_package_details_to_invoice(sender, instance, created=False, **kwargs):
    if not created:
        return

    today = date.today()
    customer = instance.tenant.service_project_link.project.customer

    invoice = models.Invoice.objects.filter(
        customer=customer,
        state=models.Invoice.States.PENDING,
        month=today.month,
        year=today.year,
    )
    if invoice.exists():
        invoice = invoice.first()
        models.OpenStackItem.objects.create(invoice=invoice, package=instance)
    else:
        models.Invoice.objects.create(customer)


def update_invoice_on_openstack_package_deletion(sender, instance, **kwargs):
    today = date.today()
    invoice = models.Invoice.objects.get(
        customer=instance.tenant.service_project_link.project.customer,
        state=models.Invoice.States.PENDING,
        month=today.month,
        year=today.year,
    )
    item = invoice.openstack_items.get(package=instance)
    item.freeze(package_deletion=True)
