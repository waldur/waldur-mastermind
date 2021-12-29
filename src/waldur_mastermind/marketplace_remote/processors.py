import logging

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.functional import cached_property

from waldur_core.core.utils import serialize_instance
from waldur_mastermind.marketplace import models, processors
from waldur_mastermind.marketplace_remote import utils
from waldur_mastermind.marketplace_remote.tasks import OrderItemStatePullTask

logger = logging.getLogger(__name__)

ResourceInvertStates = {key: val for val, key in models.Resource.States.CHOICES}


class RemoteClientMixin:
    @cached_property
    def client(self):
        return utils.get_client_for_offering(self.order_item.offering)


def build_callback_url(order_item):
    return (
        settings.WALDUR_CORE['MASTERMIND_URL']
        + '/'
        + reverse('pull_remote_order_item', kwargs={'uuid': order_item.uuid.hex})
    )


class RemoteCreateResourceProcessor(
    RemoteClientMixin, processors.BaseOrderItemProcessor
):
    def validate_order_item(self, request):
        # TODO: Implement validation
        pass

    def process_order_item(self, user):
        remote_project, _ = utils.get_or_create_remote_project(
            self.order_item.offering, self.order_item.order.project, self.client
        )
        response = self.client.marketplace_resource_create_order(
            project_uuid=remote_project['uuid'],
            offering_uuid=self.order_item.offering.backend_id,
            plan_uuid=self.order_item.plan.backend_id,
            attributes=self.order_item.attributes,
            limits=self.order_item.limits,
            callback_url=build_callback_url(self.order_item),
        )
        # NB: As a backend_id of local OrderItem, uuid of a remote Order is used
        self.order_item.backend_id = response['uuid']
        self.order_item.save()

        if settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
            utils.push_project_users(
                self.order_item.offering,
                self.order_item.order.project,
                remote_project['uuid'],
            )

        transaction.on_commit(
            lambda: OrderItemStatePullTask().apply_async(
                args=[serialize_instance(self.order_item)], kwargs={}, max_retries=19
            )
        )


class RemoteUpdateResourceProcessor(
    RemoteClientMixin, processors.BasicUpdateResourceProcessor
):
    def update_limits_process(self, user):
        response = self.client.marketplace_resource_update_limits_order(
            self.order_item.resource.backend_id,
            self.order_item.limits,
            callback_url=build_callback_url(self.order_item),
        )
        self.order_item.backend_id = response
        self.order_item.save()

        transaction.on_commit(
            lambda: OrderItemStatePullTask().apply_async(
                args=[serialize_instance(self.order_item)], kwargs={}, max_retries=19
            )
        )

        return False


class RemoteDeleteResourceProcessor(
    RemoteClientMixin, processors.BasicDeleteResourceProcessor
):
    def send_request(self, user, resource):
        response = self.client.marketplace_resource_terminate_order(
            self.order_item.resource.backend_id,
            callback_url=build_callback_url(self.order_item),
        )
        self.order_item.backend_id = response
        self.order_item.save()

        transaction.on_commit(
            lambda: OrderItemStatePullTask().apply_async(
                args=[serialize_instance(self.order_item)], kwargs={}, max_retries=19
            )
        )

        return False
