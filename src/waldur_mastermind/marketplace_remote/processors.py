import logging

from django.conf import settings
from django.utils.functional import cached_property

from waldur_mastermind.marketplace import processors
from waldur_mastermind.marketplace_remote import utils

logger = logging.getLogger(__name__)


class RemoteClientMixin:
    @cached_property
    def client(self):
        return utils.get_client_for_offering(self.order_item.offering)


class RemoteCreateResourceProcessor(
    RemoteClientMixin, processors.BasicCreateResourceProcessor
):
    def process_order_item(self, user):
        remote_project, _ = utils.get_or_create_remote_project(
            self.order_item.offering, self.order_item.order.project, self.client
        )
        response = self.client.create_resource_via_marketplace(
            project_uuid=remote_project['uuid'],
            offering_uuid=self.order_item.offering.backend_id,
            plan_uuid=self.order_item.plan.backend_id,
            attributes=self.order_item.attributes,
            limits=self.order_item.limits,
        )
        self.order_item.backend_id = response['create_order_uuid']
        self.order_item.save()
        self.create_local_resource(response['marketplace_resource_uuid'])

        if settings.WALDUR_AUTH_SOCIAL['ENABLE_EDUTEAMS_SYNC']:
            utils.push_project_users(
                self.order_item.offering,
                self.order_item.order.project,
                remote_project['uuid'],
            )


class RemoteUpdateResourceProcessor(
    RemoteClientMixin, processors.BasicUpdateResourceProcessor
):
    def update_limits_process(self, user):
        response = self.client.marketplace_resource_update_limits_order(
            self.order_item.resource.backend_id, self.order_item.limits,
        )
        self.order_item.backend_id = response
        self.order_item.save()
        return False


class RemoteDeleteResourceProcessor(
    RemoteClientMixin, processors.BasicDeleteResourceProcessor
):
    def send_request(self, user, resource):
        response = self.client.marketplace_resource_terminate_order(
            self.order_item.resource.backend_id
        )
        self.order_item.backend_id = response
        self.order_item.save()
        return False
