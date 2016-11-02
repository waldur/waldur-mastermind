from __future__ import unicode_literals

from django.utils import timezone

from nodeconductor_assembly_waldur.invoices import models as invoice_models
from nodeconductor_assembly_waldur.invoices import utils


def add_new_openstack_package_details_to_invoice(sender, instance, created=False, **kwargs):
    if not created:
        return

    datetime_now = timezone.now()
    datetime_month_end = utils.get_current_month_end_datetime()
    customer = instance.tenant.service_project_link.project.customer
    invoice, invoice_created = invoice_models.Invoice.objects.get_or_create(
        customer=customer,
        state=invoice_models.Invoice.States.PENDING,
        month=datetime_now.month,
        year=datetime_now.year
    )

    # Newly created invoice adds all customer's packages details
    # in add_openstack_packages_details_to_new_invoice handler
    if not invoice_created:
        price = instance.template.price * 24 * (datetime_month_end - datetime_now).days
        invoice.items.create(package=instance, start=datetime_now, end=datetime_month_end, price=price)


def update_invoice_on_openstack_package_deletion(sender, instance, **kwargs):
    datetime_now = timezone.now()
    invoice = invoice_models.Invoice.objects.get(
        customer=instance.tenant.service_project_link.project.customer,
        state=invoice_models.Invoice.States.PENDING,
        month=datetime_now.month,
        year=datetime_now.year
    )
    item = invoice.items.get(package=instance)

    # Recalculate invoice item price and freeze package essential fields
    item.price = instance.template.price * 24 * (datetime_now - item.start).days
    item.template_name = instance.template.name
    item.tenant_name = instance.tenant.name
    item.end = datetime_now
    item.save()
