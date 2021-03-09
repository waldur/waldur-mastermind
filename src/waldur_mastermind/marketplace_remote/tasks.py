from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from waldur_core.structure.models import Project
from waldur_core.structure.tasks import BackgroundListPullTask, BackgroundPullTask
from waldur_mastermind.invoices.models import Invoice, InvoiceItem
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_remote import PLUGIN_NAME
from waldur_mastermind.marketplace_remote.constants import (
    INVOICE_ITEM_FIELDS,
    OFFERING_FIELDS,
)
from waldur_mastermind.marketplace_remote.utils import (
    get_client_for_offering,
    pull_fields,
)

OrderItemInvertStates = {key: val for val, key in models.OrderItem.States.CHOICES}


class OfferingPullTask(BackgroundPullTask):
    def pull(self, local_offering):
        client = get_client_for_offering(local_offering)
        remote_offering = client.get_marketplace_offering(local_offering.backend_id)
        pull_fields(OFFERING_FIELDS, local_offering, remote_offering)


class OfferingListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_offerings'
    pull_task = OfferingPullTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(type=PLUGIN_NAME)


class OrderItemPullTask(BackgroundPullTask):
    def pull(self, local_order_item):
        client = get_client_for_offering(local_order_item.offering)
        remote_order_item = client.get_order_item(local_order_item.backend_id)

        if remote_order_item['state'] != local_order_item.get_state_display():
            local_order_item.state = OrderItemInvertStates[remote_order_item['state']]
            local_order_item.save()
        pull_fields(('error_message',), local_order_item, remote_order_item)


class OrderItemListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_order_items'
    pull_task = OrderItemPullTask

    def get_pulled_objects(self):
        return models.OrderItem.objects.filter(offering__type=PLUGIN_NAME).exclude(
            state__in=models.OrderItem.States.TERMINAL_STATES
        )


class InvoicePullTask(BackgroundPullTask):
    def pull(self, local_offering):
        customer_uuid = local_offering.secret_options['customer_uuid']
        client = get_client_for_offering(local_offering)

        now = timezone.now()
        remote_invoice = client.get_invoice_for_customer(
            customer_uuid, now.year, now.month
        )
        local_invoice = Invoice.objects.get(
            customer=local_offering.customer, year=now.year, month=now.month
        )

        for remote_item in remote_invoice['items']:
            self.pull_invoice_item(remote_item, local_offering, local_invoice)

    def pull_invoice_item(self, remote_item, local_offering, local_invoice):
        try:
            local_resource = models.Resource.objects.get(
                offering=local_offering, backend_id=remote_item['resource_uuid']
            )
        except ObjectDoesNotExist:
            return

        try:
            local_project = Project.objects.get(uuid=remote_item['project_backend_id'])
        except ObjectDoesNotExist:
            return

        defaults = {key: remote_item[key] for key in INVOICE_ITEM_FIELDS}
        defaults['resource'] = local_resource
        defaults['project'] = local_project

        InvoiceItem.objects.update_or_create(
            invoice=local_invoice, backend_id=remote_item['uuid'], defaults=defaults,
        )


class InvoiceListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_invoices'
    pull_task = InvoicePullTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(type=PLUGIN_NAME)
