from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from waldur_mastermind.common.utils import mb_to_gb, parse_datetime
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.utils import get_current_month_end, get_full_days
from waldur_mastermind.marketplace.registrators import MarketplaceRegistrator
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_TYPE,
    TENANT_TYPE,
)

from . import utils

component_factors = {STORAGE_TYPE: 1024, RAM_TYPE: 1024}


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

    def create_component_item(self, source, plan_component, invoice, start, end):
        offering_component = plan_component.component
        limit = source.limits.get(offering_component.type, 0)
        if not limit:
            return
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
            article_code=offering_component.article_code or source.plan.article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
            measured_unit=offering_component.measured_unit,
        )

    def update_component_item(self, source, component_type, invoice, new_quantity):
        invoice_item = invoices_models.InvoiceItem.objects.get(
            resource=source,
            details__offering_component_type=component_type,
            invoice=invoice,
        )
        resource_limit_periods = invoice_item.details['resource_limit_periods']
        old_period = resource_limit_periods.pop()
        old_quantity = int(old_period['quantity'])
        old_start = parse_datetime(old_period['start'])
        today = timezone.now()
        new_quantity /= component_factors.get(component_type, 1)
        new_quantity = int(new_quantity)
        if old_quantity == new_quantity:
            # Skip update if limit is the same
            return
        if old_quantity > new_quantity:
            old_end = today.replace(hour=23, minute=59, second=59)
            new_start = old_end + timedelta(seconds=1)
        else:
            new_start = today.replace(hour=0, minute=0, second=0)
            old_end = new_start - timedelta(seconds=1)
        old_period = utils.serialize_resource_limit_period(
            {'start': old_start, 'end': old_end, 'quantity': old_quantity}
        )
        new_period = utils.serialize_resource_limit_period(
            {
                'start': new_start,
                'end': get_current_month_end(),
                'quantity': new_quantity,
            }
        )
        resource_limit_periods.extend([old_period, new_period])
        invoice_item.quantity = sum(
            period['quantity']
            * get_full_days(
                parse_datetime(period['start']), parse_datetime(period['end']),
            )
            for period in resource_limit_periods
        )
        invoice_item.save(update_fields=['details', 'quantity'])

    @transaction.atomic
    def create_or_update_component_item(
        self, source, invoice, component_type, quantity
    ):
        if invoices_models.InvoiceItem.objects.filter(
            resource=source,
            details__offering_component_type=component_type,
            invoice=invoice,
        ).exists():
            self.update_component_item(source, component_type, invoice, quantity)
        else:
            start = timezone.now()
            end = get_current_month_end()
            plan_component = source.plan.components.get(component__type=component_type)
            self.create_component_item(source, plan_component, invoice, start, end)

    def _create_item(self, source, invoice, start, end, **kwargs):
        for plan_component in source.plan.components.all():
            self.create_component_item(source, plan_component, invoice, start, end)
