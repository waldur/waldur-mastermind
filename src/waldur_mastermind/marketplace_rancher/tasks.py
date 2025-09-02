from typing import cast

from celery import shared_task

from waldur_core.core.utils import deserialize_instance
from waldur_mastermind.invoices.models import InvoiceItem
from waldur_mastermind.invoices.utils import get_current_month, get_current_year
from waldur_mastermind.marketplace.models import Resource

from ..marketplace.enums import MANAGED_RANCHER_OFFERING
from . import utils


@shared_task(
    name="waldur_mastermind.marketplace_rancher.sync_managed_rancher_invoice_items"
)
def sync_managed_rancher_invoice_items():
    year, month = get_current_year(), get_current_month()
    for downstream_invoice_item in InvoiceItem.objects.filter(
        resource__offering__type=MANAGED_RANCHER_OFFERING,
        invoice__year=year,
        invoice__month=month,
        backend_uuid__isnull=False,
    ):
        try:
            upstream_invoice_item = InvoiceItem.objects.get(
                uuid=downstream_invoice_item.backend_uuid
            )
        except InvoiceItem.DoesNotExist:
            continue
        utils.sync_managed_rancher_invoice_items(
            upstream_invoice_item, downstream_invoice_item
        )
        utils.sync_aggregated_invoice_item(
            upstream_invoice_item, downstream_invoice_item
        )


@shared_task(name="waldur_mastermind.marketplace_rancher.update_tenant_limits")
def update_tenant_limits(serialized_tenant_resource: str, new_limits: dict):
    tenant_resource = cast(Resource, deserialize_instance(serialized_tenant_resource))
    utils.submit_update_order(tenant_resource, new_limits)
