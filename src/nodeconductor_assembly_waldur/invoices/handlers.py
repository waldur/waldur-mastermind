from __future__ import unicode_literals

from django.utils import timezone

from nodeconductor.core import utils as core_utils

from . import models


def add_new_openstack_package_details_to_invoice(sender, instance, created=False, **kwargs):
    if not created:
        return

    now = timezone.now()
    customer = instance.tenant.service_project_link.project.customer
    invoice, created = models.Invoice.objects.get_or_create_with_items(
        customer=customer,
        month=now.month,
        year=now.year,
    )
    if not created:
        end_datetime = core_utils.month_end(now)
        models.OpenStackItem.objects.create_with_price(invoice=invoice, package=instance,
                                                       start_datetime=now, end_datetime=end_datetime)
    else:
        item = invoice.openstack_items.get(package=instance)
        item.recalculate_price(now)


def update_invoice_on_openstack_package_deletion(sender, instance, **kwargs):
    end_datetime = timezone.now()
    item = models.OpenStackItem.objects.get(
        package=instance,
        invoice__customer=instance.tenant.service_project_link.project.customer,
        invoice__state=models.Invoice.States.PENDING,
        invoice__year=end_datetime.year,
        invoice__month=end_datetime.month,
    )
    item.freeze(end_datetime=end_datetime, package_deletion=True)
