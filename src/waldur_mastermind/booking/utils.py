import copy
import datetime
import logging

from dateutil.parser import parse as parse_datetime
from django.utils import timezone

from waldur_mastermind.marketplace import models as marketplace_models

from . import PLUGIN_NAME

logger = logging.getLogger(__name__)


class TimePeriod:
    def __init__(self, start, end, period_id=None):
        if not isinstance(start, datetime.datetime):
            start = parse_datetime(start)

        if not isinstance(end, datetime.datetime):
            end = parse_datetime(end)

        self.start = start
        self.end = end

        if period_id:
            self.id = period_id


def is_interval_in_schedules(interval, schedules):
    for s in schedules:
        if interval.start >= s.start:
            if interval.end <= s.end:
                return True

    return False


def get_offering_bookings(offering):
    """
    OK means that booking request has been accepted.
    CREATING means that booking request has been made but not yet confirmed.
    If it is rejected, the time slot could be freed up.
    But it is more end-user friendly if choices that you see are
    always available (if some time slots at risk, better to conceal them).
    """
    States = marketplace_models.Resource.States
    schedules = marketplace_models.Resource.objects.filter(
        offering=offering, state__in=(States.OK, States.CREATING),
    ).values_list('attributes__schedules', flat=True)
    return [
        TimePeriod(period['start'], period['end'], period.get('id'))
        for schedule in schedules
        if schedule
        for period in schedule
        if period
    ]


def get_info_about_upcoming_bookings():
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    upcoming_bookings = marketplace_models.Resource.objects.filter(
        offering__type=PLUGIN_NAME,
        state=marketplace_models.Resource.States.OK,
        attributes__schedules__0__start__icontains='%s-%02d-%02dT'
        % (tomorrow.year, tomorrow.month, tomorrow.day),
    )

    result = []

    for resource in upcoming_bookings:
        try:
            order_item = marketplace_models.OrderItem.objects.get(
                resource=resource, type=marketplace_models.OrderItem.Types.CREATE
            )
            user = order_item.order.created_by
        except marketplace_models.OrderItem.DoesNotExist:
            logger.warning(
                'Skipping notification because '
                'marketplace resource hasn\'t got a order item. '
                'Resource ID: %s',
                resource.id,
            )
        except marketplace_models.OrderItem.MultipleObjectsReturned:
            logger.warning(
                'Skipping notification because '
                'marketplace resource has got few order items. '
                'Resource ID: %s',
                resource.id,
            )
        else:
            rows = list(
                filter(lambda x: x['user'] == resource.project.customer, result)
            )
            if rows:
                rows[0]['resources'].append(resource)
            else:
                result.append(
                    {'user': user, 'resources': [resource],}
                )

    return result


def change_attributes_for_view(attrs):
    # We use copy of attrs to do not change offering.attributes
    attributes = copy.deepcopy(attrs)
    schedules = attributes.get('schedules', [])
    attributes['schedules'] = [
        schedule
        for schedule in schedules
        if parse_datetime(schedule['end']) > timezone.now()
    ]
    for schedule in attributes['schedules']:
        if parse_datetime(schedule['start']) < timezone.now():
            schedule['start'] = timezone.now().strftime('%Y-%m-%dT%H:%M:%S.000Z')

    return attributes
