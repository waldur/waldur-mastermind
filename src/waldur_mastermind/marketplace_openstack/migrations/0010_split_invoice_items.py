import decimal
from collections import defaultdict
from datetime import timedelta

from django.db import migrations
from django.utils import timezone

TENANT_TYPE = 'Packages.Template'
RAM_TYPE = 'ram'
CORES_TYPE = 'cores'
STORAGE_TYPE = 'storage'

component_factors = {STORAGE_TYPE: 1024, RAM_TYPE: 1024}


def get_full_days(start, end):
    seconds_in_day = 24 * 60 * 60
    full_days, extra_seconds = divmod((end - start).total_seconds(), seconds_in_day)
    if extra_seconds > 0:
        full_days += 1

    return int(full_days)


def quantize_price(value):
    return value.quantize(decimal.Decimal('0.01'), rounding=decimal.ROUND_UP)


def get_resource_name(resource):
    if resource.plan:
        return '%s (%s / %s)' % (
            resource.name,
            resource.offering.name,
            resource.plan.name,
        )
    else:
        return '%s (%s)' % (resource.name, resource.offering.name)


def get_invoice_item_name(source, component_type):
    resource_name = get_resource_name(source)
    if component_type == CORES_TYPE:
        return f'{resource_name} / CPU'
    elif component_type == RAM_TYPE:
        return f'{resource_name} / RAM'
    elif component_type == STORAGE_TYPE:
        return f'{resource_name} / storage'
    elif component_type.startswith('gigabytes_'):
        return f'{resource_name} / {component_type.replace("gigabytes_", "")} storage'
    else:
        return resource_name


def get_component_details(resource, plan_component):
    customer = resource.offering.customer
    service_provider = getattr(customer, 'serviceprovider', None)

    return {
        'resource_name': resource.name,
        'resource_uuid': resource.uuid.hex,
        'plan_name': resource.plan.name if resource.plan else '',
        'plan_uuid': resource.plan.uuid.hex if resource.plan else '',
        'offering_type': resource.offering.type,
        'offering_name': resource.offering.name,
        'offering_uuid': resource.offering.uuid.hex,
        'service_provider_name': customer.name,
        'service_provider_uuid': ''
        if not service_provider
        else service_provider.uuid.hex,
        'plan_component_id': plan_component.id,
        'offering_component_type': plan_component.component.type,
        'offering_component_name': plan_component.component.name,
    }


def collect_limit_periods(resource_invoice_items):
    resource_limit_periods = defaultdict(list)
    for invoice_item in resource_invoice_items:
        for limit_name, limit_value in invoice_item.details['limits'].items():
            factor = component_factors.get(limit_name, 1)
            quantity = decimal.Decimal(limit_value / factor)
            resource_limit_periods[limit_name].append(
                {
                    'start': invoice_item.start,
                    'end': invoice_item.end,
                    'quantity': quantity,
                }
            )
    return resource_limit_periods


def merge_consecutive_periods(resource_limit_periods):
    output = {}
    for limit_name, limit_periods in resource_limit_periods.items():
        limit_periods = list(sorted(limit_periods, key=lambda period: period['end']))
        if len(limit_periods) == 1:
            output[limit_name] = limit_periods
            continue
        prev_value = limit_periods[0]['quantity']
        prev_end = limit_periods[0]['end']
        merged_limit_periods = [limit_periods[0]]
        for limit_period in limit_periods[1:]:
            if limit_period['quantity'] == prev_value and limit_period[
                'start'
            ] - prev_end == timedelta(seconds=1):
                # Extend period ie merge consecutive items
                merged_limit_periods[-1]['end'] = limit_period['end']
            else:
                merged_limit_periods.append(limit_period)
            prev_end = limit_period['end']
            prev_value = limit_period['quantity']
        output[limit_name] = merged_limit_periods
    return output


def serialize_resource_limit_period(period):
    billing_periods = get_full_days(period['start'], period['end'])
    return {
        'start': period['start'].isoformat(),
        'end': period['end'].isoformat(),
        'quantity': str(period['quantity']),
        'billing_periods': billing_periods,
        'total': str(period['quantity'] * billing_periods),
    }


def create_invoice_items_for_components(
    InvoiceItem, invoice, resource, resource_invoice_items
):
    resource_limit_periods = collect_limit_periods(resource_invoice_items)
    resource_limit_periods = merge_consecutive_periods(resource_limit_periods)

    new_invoice_items = []
    for plan_component in resource.plan.components.all():
        offering_component = plan_component.component
        component_type = offering_component.type
        periods = resource_limit_periods.get(component_type)
        if not periods:
            print(
                f'Skipping plan component {component_type} of '
                f'resource {resource.id} because resource_limit_periods list is empty.'
            )
            continue
        quantity = sum(
            period['quantity'] * get_full_days(period['start'], period['end'])
            for period in periods
        )
        if not quantity:
            print(
                f'Skipping plan component {component_type} of '
                f'resource {resource.id} because aggregated quantity is zero.'
            )
            continue
        details = get_component_details(resource, plan_component)
        details['resource_limit_periods'] = list(
            map(serialize_resource_limit_period, resource_limit_periods[component_type])
        )
        new_invoice_item = InvoiceItem.objects.create(
            name=get_invoice_item_name(resource, component_type),
            unit_price=plan_component.price,
            article_code=offering_component.article_code,
            measured_unit=f'{offering_component.measured_unit}*day',
            resource=resource,
            project=resource.project,
            unit='quantity',
            quantity=quantity,
            invoice=invoice,
            start=min(period['start'] for period in periods),
            end=max(period['end'] for period in periods),
            details=details,
        )
        new_invoice_items.append(new_invoice_item)
    return new_invoice_items


def format_items_list(items):
    return ', '.join(str(item.id) for item in items)


def process_invoices(apps, schema_editor):
    Invoice = apps.get_model('invoices', 'Invoice')
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    Resource = apps.get_model('marketplace', 'Resource')
    today = timezone.now()
    for invoice in Invoice.objects.filter(year=today.year, month=today.month):
        invoice_items = InvoiceItem.objects.filter(
            invoice=invoice,
            resource__offering__type=TENANT_TYPE,
            details__has_key='limits',
        )
        if not invoice_items.exists():
            continue
        resource_ids = invoice_items.values_list('resource_id', flat=True)
        for resource_id in resource_ids:
            resource = Resource.objects.get(id=resource_id)
            # Cache old_invoice_items so that they are not reevaluated later
            old_invoice_items = list(invoice_items.filter(resource_id=resource_id))
            new_invoice_items = create_invoice_items_for_components(
                InvoiceItem, invoice, resource, old_invoice_items
            )
            if new_invoice_items:
                print(
                    f"Replacing resource items {format_items_list(old_invoice_items)} "
                    f"with component-based items {format_items_list(new_invoice_items)}"
                )
                for old_invoice_item in old_invoice_items:
                    old_invoice_item.delete()


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace_openstack', '0009_fill_project'),
        ('invoices', '0052_delete_servicedowntime'),
    ]

    operations = [migrations.RunPython(process_invoices, atomic=True)]
