import logging

from django.core.management.base import BaseCommand
from rest_framework import status

from waldur_core.core.models import User
from waldur_core.logging.views import EventViewSet
from waldur_mastermind.common.utils import get_request
from waldur_mastermind.marketplace.models import Order, OrderItem, Resource


logger = logging.getLogger(__name__)


def get_resource_user(default_user, resource):
    if not resource.scope:
        logger.warning('Using default user for resource because it does not have scope. '
                       'Resource UUID: %s', resource.uuid)
        return default_user
    view = EventViewSet.as_view({'get': 'list'})
    response = get_request(view, default_user,
                           event_type='resource_creation_scheduled',
                           resource_type=resource.scope.get_scope_type(),
                           resource_uuid=resource.scope.uuid)
    if response.status_code == status.HTTP_200_OK and len(response.data) > 0:
        user_uuid = response.data[0]['user_uuid']
        try:
            return User.objects.get(uuid=user_uuid)
        except User.DoesNotExist:
            logger.warning('Using default user for resource because user does not exist. '
                           'Resource UUID: %s. User UUID: %s', resource.uuid, user_uuid)
    else:
        logger.warning('Using default user for resource because related event is not found. '
                       'Resource UUID: %s', resource.uuid)
    return default_user


def import_orders(find_user=True):
    default_user = User.objects.filter(is_staff=True).first()
    existing_resources = OrderItem.objects.exclude(resource_id=None) \
        .values_list('resource_id', flat=True).distinct()
    missing_resources = Resource.objects.exclude(id__in=existing_resources)
    for resource in missing_resources:
        user = find_user and get_resource_user(default_user, resource) or default_user
        order = Order.objects.create(
            created=resource.created,
            modified=resource.modified,
            created_by=user,
            approved_by=user,
            approved_at=resource.created,
            project=resource.project,
            state=Order.States.DONE,
        )
        OrderItem.objects.create(
            order=order,
            resource=resource,
            offering=resource.offering,
            attributes=resource.attributes,
            limits=resource.limits,
            plan=resource.plan,
            state=OrderItem.States.DONE,
        )
    return missing_resources.count()


class Command(BaseCommand):
    help = """Create marketplace order for each resource if it does not yet exist."""

    def handle(self, *args, **options):
        count = import_orders()
        if count == 0:
            self.stdout.write(self.style.SUCCESS('There are no resources without orders.'))
        if count == 1:
            self.stdout.write(self.style.SUCCESS('%s order has been created.' % count))
        else:
            self.stdout.write(self.style.SUCCESS('%s orders have been created.' % count))
