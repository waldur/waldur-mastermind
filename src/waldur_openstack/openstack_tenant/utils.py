from django.contrib.contenttypes.models import ContentType

from waldur_core.cost_tracking import ConsumableItem
from waldur_core.cost_tracking.models import DefaultPriceListItem

from . import models, PriceItemTypes


def get_consumable_item(flavor_name):
    return ConsumableItem(item_type=PriceItemTypes.FLAVOR, key=flavor_name, name='Flavor: %s' % flavor_name)


def sync_price_list_item(flavor):
    resource_content_type = ContentType.objects.get_for_model(models.Instance)
    consumable_item = get_consumable_item(flavor.name)
    DefaultPriceListItem._create_or_update_default_price_list_item(
        resource_content_type=resource_content_type,
        consumable_item=consumable_item,
    )


def get_valid_availability_zones(instance):
    """
    Fetch valid availability zones for instance or volume from shared settings.
    """
    tenant = instance.service_settings.scope
    if tenant:
        return tenant.service_settings.options.get('valid_availability_zones') or {}
    return {}
