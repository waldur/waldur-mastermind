from django.contrib.contenttypes.models import ContentType

from waldur_core.cost_tracking import models as cost_tracking_models


def get_service_price_list_items(service, resource_model):
    """ Return all price list item that belongs to given service """
    resource_content_type = ContentType.objects.get_for_model(resource_model)
    default_items = set(cost_tracking_models.DefaultPriceListItem.objects.filter(
        resource_content_type=resource_content_type))
    items = set(cost_tracking_models.PriceListItem.objects.filter(
        default_price_list_item__in=default_items, service=service).select_related('default_price_list_item'))
    rewrited_defaults = set([i.default_price_list_item for i in items])
    return items | (default_items - rewrited_defaults)
