from waldur_mastermind.common.utils import mb_to_gb
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.utils import get_full_days
from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_TYPE,
    TENANT_TYPE,
)

from . import utils


class OpenStackRegistrator(MarketplaceRegistrator):
    plugin_name = TENANT_TYPE

    def _get_invoice_item_name(self, source, plan_component):
        component_type = plan_component.component.type
        resource_name = self.get_name(source)
        limit = source.limits.get(component_type, 0)
        if component_type == CORES_TYPE:
            return f'{resource_name} / {int(limit)} CPU'
        elif component_type == RAM_TYPE:
            return f'{resource_name} / {int(mb_to_gb(limit))} RAM'
        elif component_type == STORAGE_TYPE:
            return f'{resource_name} / {int(mb_to_gb(limit))} storage'
        elif component_type.startswith('gigabytes_'):
            return f'{resource_name} / {int(limit)} {component_type.replace("gigabytes_", "")} storage'
        else:
            return resource_name

    def _create_item(self, source, invoice, start, end, **kwargs):
        component_factors = {STORAGE_TYPE: 1024, RAM_TYPE: 1024}

        for plan_component in source.plan.components.all():
            offering_component = plan_component.component
            limit = source.limits.get(offering_component.type, 0)
            if not limit:
                continue
            details = self.get_component_details(source, plan_component)
            quantity = limit / component_factors.get(offering_component.type, 1)
            details['resource_limit_periods'] = [
                utils.serialize_resource_limit_period(
                    {'start': start, 'end': end, 'quantity': quantity}
                )
            ]

            invoices_models.InvoiceItem.objects.create(
                name=self._get_invoice_item_name(source, plan_component),
                resource=source,
                project=source.project,
                unit_price=plan_component.price,
                unit=invoices_models.InvoiceItem.Units.PER_DAY,
                quantity=quantity * get_full_days(start, end),
                article_code=offering_component.article_code
                or source.plan.article_code,
                invoice=invoice,
                start=start,
                end=end,
                details=details,
                measured_unit=offering_component.measured_unit,
            )
