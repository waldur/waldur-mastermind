from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from waldur_core.cost_tracking.models import PriceEstimate
from waldur_core.structure.models import ServiceSettings


def get_total_cost():
    points = []
    estimates, mapping = get_current_estimates()
    for estimate in estimates.only('consumed', 'object_id'):
        points.append({
            'measurement': 'total_cost',
            'tags': {
                'provider': mapping[estimate.object_id],
            },
            'fields': {
                'value': estimate.consumed,
            },
        })
    return points


def get_current_estimates():
    content_type = ContentType.objects.get_for_model(ServiceSettings)
    service_settings = ServiceSettings.objects.filter(shared=True).only('pk', 'name')
    mapping = {item.id: item.name for item in service_settings}
    now = timezone.now()
    estimates = PriceEstimate.objects.filter(
        content_type=content_type,
        object_id__in=mapping.keys(),
        month=now.month,
        year=now.year,
    )
    return estimates, mapping
