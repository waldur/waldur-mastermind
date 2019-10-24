import datetime

from django.utils.dateparse import parse_datetime
import logging

from waldur_mastermind.marketplace import models as marketplace_models

from . import PLUGIN_NAME

logger = logging.getLogger(__name__)


class TimePeriod:
    def __init__(self, start, end):
        if not isinstance(start, datetime.datetime):
            start = parse_datetime(start)

        if not isinstance(end, datetime.datetime):
            end = parse_datetime(end)

        self.start = start
        self.end = end


def is_interval_in_schedules(interval, schedules):
    for s in schedules:
        if interval.start >= s.start:
            if interval.end <= s.end:
                return True

    return False


def get_info_about_upcoming_bookings():
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    upcoming_bookings = marketplace_models.Resource.objects.filter(
        offering__type=PLUGIN_NAME,
        state=marketplace_models.Resource.States.OK,
        attributes__schedules__0__start__icontains='%s-%02d-%02dT' % (tomorrow.year, tomorrow.month, tomorrow.day))

    result = []

    for resource in upcoming_bookings:
        try:
            order_item = marketplace_models.OrderItem.objects.get(
                resource=resource,
                type=marketplace_models.OrderItem.Types.CREATE)
            user = order_item.order.created_by
        except marketplace_models.OrderItem.DoesNotExist:
            logger.warning('Skipping notification because '
                           'marketplace resource hasn\'t got a order item. '
                           'Resource ID: %s', resource.id)
        except marketplace_models.OrderItem.MultipleObjectsReturned:
            logger.warning('Skipping notification because '
                           'marketplace resource has got few order items. '
                           'Resource ID: %s', resource.id)
        else:
            rows = list(filter(lambda x: x['user'] == resource.project.customer, result))
            if rows:
                rows[0]['resources'].append(resource)
            else:
                result.append({
                    'user': user,
                    'resources': [resource],
                })

    return result
