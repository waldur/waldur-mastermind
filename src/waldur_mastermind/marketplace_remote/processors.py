import logging

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.functional import cached_property
from rest_framework import serializers

from waldur_core.core.utils import serialize_instance
from waldur_mastermind.marketplace import models, processors
from waldur_mastermind.marketplace_remote import utils
from waldur_mastermind.marketplace_remote.tasks import OrderStatePullTask

logger = logging.getLogger(__name__)

ResourceInvertStates = {key: val for val, key in models.Resource.States.CHOICES}


class RemoteClientMixin:
    @cached_property
    def client(self):
        return utils.get_client_for_offering(self.order.offering)


def build_callback_url(order):
    return settings.WALDUR_CORE['MASTERMIND_URL'] + reverse(
        'pull_remote_order', kwargs={'uuid': order.uuid.hex}
    )


class RemoteCreateResourceProcessor(RemoteClientMixin, processors.BaseOrderProcessor):
    def validate_order(self, request):
        # TODO: Implement validation
        pass

    def process_order(self, user):
        remote_project, _ = utils.get_or_create_remote_project(
            self.order.offering, self.order.project, self.client
        )
        response = self.client.marketplace_resource_create_order(
            project_uuid=remote_project['uuid'],
            offering_uuid=self.order.offering.backend_id,
            plan_uuid=self.order.plan.backend_id,
            attributes=self.order.attributes,
            limits=self.order.limits,
            callback_url=build_callback_url(self.order),
        )
        # NB: As a backend_id of local Order, uuid of a remote Order is used
        self.order.backend_id = response['uuid']
        self.order.save()

        if settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
            utils.push_project_users(
                self.order.offering,
                self.order.project,
                remote_project['uuid'],
            )

        transaction.on_commit(
            lambda: OrderStatePullTask().apply_async(
                args=[serialize_instance(self.order)], kwargs={}, max_retries=19
            )
        )


class RemoteUpdateResourceProcessor(
    RemoteClientMixin, processors.BasicUpdateResourceProcessor
):
    def update_limits_process(self, user):
        response = self.client.marketplace_resource_update_limits_order(
            self.order.resource.backend_id,
            self.order.limits,
            callback_url=build_callback_url(self.order),
        )
        self.order.backend_id = response
        self.order.save()

        transaction.on_commit(
            lambda: OrderStatePullTask().apply_async(
                args=[serialize_instance(self.order)], kwargs={}, max_retries=19
            )
        )

        return False


class RemoteDeleteResourceProcessor(
    RemoteClientMixin, processors.BasicDeleteResourceProcessor
):
    def validate_order_item(self, request):
        if not self.order_item.resource.backend_id:
            raise serializers.ValidationError('Resource does not have backend ID.')

    def send_request(self, user, resource):
        # If terminate order already exists in the remote side,
        # it should be imported and local order is switched to erred.
        imported_orders = utils.import_resource_orders(resource)
        if imported_orders:
            utils.pull_resource_state(resource)
        if any(item.type == models.Order.Types.TERMINATE for item in imported_orders):
            self.order.set_state_erred()
            self.order.error_message = 'Another order exists already.'
            self.order.save()
            return False

        response = self.client.marketplace_resource_terminate_order(
            self.order.resource.backend_id,
            callback_url=build_callback_url(self.order),
        )
        self.order.backend_id = response
        self.order.save()

        transaction.on_commit(
            lambda: OrderStatePullTask().apply_async(
                args=[serialize_instance(self.order)], kwargs={}, max_retries=19
            )
        )

        return False
