from django.core.exceptions import ObjectDoesNotExist

from waldur_core.structure.tasks import BackgroundListPullTask, BackgroundPullTask
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_remote import PLUGIN_NAME
from waldur_mastermind.marketplace_remote.constants import OFFERING_FIELDS
from waldur_mastermind.marketplace_remote.utils import (
    get_client_for_offering,
    pull_fields,
)

OrderItemInvertStates = {key: val for val, key in models.OrderItem.States.CHOICES}


class OfferingPullTask(BackgroundPullTask):
    def pull(self, local_offering):
        client = get_client_for_offering(local_offering)
        remote_offering = client.get_marketplace_offering(local_offering.backend_id)
        pull_fields(OFFERING_FIELDS, local_offering, remote_offering)


class OfferingListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_offerings'
    pull_task = OfferingPullTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(type=PLUGIN_NAME)


class OrderItemPullTask(BackgroundPullTask):
    def pull(self, local_order_item):
        client = get_client_for_offering(local_order_item.offering)
        remote_order_item = client.get_order_item(local_order_item.backend_id)

        if remote_order_item['state'] != local_order_item.get_state_display():
            local_order_item.state = OrderItemInvertStates[remote_order_item['state']]
            local_order_item.save()
        pull_fields(('error_message',), local_order_item, remote_order_item)


class OrderItemListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_order_items'
    pull_task = OrderItemPullTask

    def get_pulled_objects(self):
        return models.OrderItem.objects.filter(offering__type=PLUGIN_NAME).exclude(
            state__in=models.OrderItem.States.TERMINAL_STATES
        )


class UsagePullTask(BackgroundPullTask):
    def pull(self, local_resource: models.Resource):
        client = get_client_for_offering(local_resource.offering)

        remote_usages = client.list_component_usages(local_resource.backend_id)

        for remote_usage in remote_usages:
            try:
                offering_component = models.OfferingComponent.objects.get(
                    offering=local_resource.offering, type=remote_usage['type']
                )
            except ObjectDoesNotExist:
                continue
            defaults = {
                'usage': remote_usage['usage'],
                'name': remote_usage['name'],
                'description': remote_usage['description'],
                'created': remote_usage['created'],
                'date': remote_usage['date'],
                'billing_period': remote_usage['billing_period'],
            }
            models.ComponentUsage.objects.update_or_create(
                resource=local_resource,
                backend_id=remote_usage['uuid'],
                component=offering_component,
                defaults=defaults,
            )


class UsageListPullTask(BackgroundListPullTask):
    name = 'waldur_mastermind.marketplace_remote.pull_usage'
    pull_task = UsagePullTask

    def get_pulled_objects(self):
        return models.Resource.objects.filter(offering__type=PLUGIN_NAME)
