from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from waldur_core.cost_tracking import ConsumableItem
from waldur_core.cost_tracking.models import DefaultPriceListItem
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.packages import models as packages_models
from waldur_openstack.openstack import models as openstack_models


class Types(object):
    PACKAGE_TEMPLATE = 'PackageTemplate'


def get_consumable_item(package_template):
    return ConsumableItem(
        item_type=Types.PACKAGE_TEMPLATE,
        key=package_template.name,
        default_price=package_template.price / 24)  # default price per hour


def sync_price_list_item(package_template):
    resource_content_type = ContentType.objects.get_for_model(openstack_models.Tenant)
    consumable_item = get_consumable_item(package_template)
    DefaultPriceListItem._create_or_update_default_price_list_item(
        resource_content_type=resource_content_type,
        consumable_item=consumable_item,
    )


def get_openstack_items():
    model_type = ContentType.objects.get_for_model(packages_models.OpenStackPackage)
    return invoices_models.GenericInvoiceItem.objects.filter(content_type=model_type)


def shift_backward(item, days=1):
    """
    Shifts end date to N 'days' ago.
    If N is larger than it lasts - zero length will be set.
    :param days: number of days to shift end date
    """
    if (item.end - item.start).days > days:
        end = item.end - timezone.timedelta(days=1)
    else:
        end = item.start

    item.end = end
    item.save()


def extend_to_the_end_of_the_day(item):
    item.end = item.end.replace(hour=23, minute=59, second=59)
    item.save()


def get_invoice_item_name(package):
    template_category = package.template.get_category_display()
    tenant_name = package.tenant.name
    template_name = package.template.name

    if template_category:
        return '%s (%s / %s)' % (tenant_name, template_category, template_name)
    else:
        return '%s (%s)' % (tenant_name, template_name)
