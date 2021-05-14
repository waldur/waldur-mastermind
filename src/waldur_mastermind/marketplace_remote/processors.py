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
    def send_request(self, user):
        remote_project, _ = utils.get_or_create_remote_project(
            self.order_item.offering, self.order_item.order.project, self.client
        )
        response = self.client.marketplace_resource_create(
            project_uuid=remote_project['uuid'],
            offering_uuid=self.order_item.offering.backend_id,
            plan_uuid=self.order_item.plan.backend_id,
            attributes=self.order_item.attributes,
            limits=self.order_item.limits,
        )
        self.order_item.backend_id = response['uuid']
        self.order_item.save()

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
        response = self.client.marketplace_resource_update_limits(
            self.order_item.resource.backend_id, self.order_item.limits,
        )
        self.order_item.backend_id = response['uuid']
        self.order_item.save()


class RemoteDeleteResourceProcessor(
    RemoteClientMixin, processors.BasicDeleteResourceProcessor
):
    def send_request(self, user, resource):
        response = self.client.marketplace_resource_terminate(
            self.order_item.resource.backend_id
        )
        self.order_item.backend_id = response['uuid']
        self.order_item.save()
        return True
