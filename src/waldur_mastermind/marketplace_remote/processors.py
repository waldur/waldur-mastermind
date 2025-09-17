import logging
from typing import cast
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from waldur_api_client.api.marketplace_orders import marketplace_orders_create
from waldur_api_client.api.marketplace_resources import (
    marketplace_resources_retrieve,
    marketplace_resources_terminate,
    marketplace_resources_update_limits,
)
from waldur_api_client.errors import UnexpectedStatus
from waldur_api_client.models.order_create_request import OrderCreateRequest
from waldur_api_client.models.order_create_request_limits import (
    OrderCreateRequestLimits,
)
from waldur_api_client.models.resource_terminate_request import ResourceTerminateRequest
from waldur_api_client.models.resource_update_limits_request import (
    ResourceUpdateLimitsRequest,
)
from waldur_api_client.models.resource_update_limits_request_limits import (
    ResourceUpdateLimitsRequestLimits,
)
from waldur_api_client.types import UNSET

from waldur_core.core.models import User
from waldur_core.core.utils import serialize_instance
from waldur_mastermind.marketplace import models, processors
from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.marketplace_remote import utils
from waldur_mastermind.marketplace_remote.tasks import OrderStatePullTask

logger = logging.getLogger(__name__)


MAX_RETRIES = 19


def build_callback_url(order: models.Order):
    base_url = settings.WALDUR_CORE["MASTERMIND_URL"]  # type: ignore
    return base_url + reverse("pull_remote_order", kwargs={"uuid": order.uuid.hex})


class RemoteCreateResourceProcessor(processors.BaseOrderProcessor):
    def validate_order(self, request):
        # TODO: Implement validation
        pass

    def process_order(self, user: User):
        client = utils.get_client_for_offering(self.order.offering)
        remote_project, _ = utils.get_or_create_remote_project(
            self.order.offering, self.order.project, client
        )
        remote_project_uuid = cast(UUID, remote_project.uuid).hex
        # To bypass the api check we convert the attributes to a generic object with to_dict method
        converted_attributes = utils.GenericOrderAttribute(self.order.attributes)
        response = marketplace_orders_create.sync(
            client=client,
            body=OrderCreateRequest(
                project=f"{client._base_url}/api/projects/{remote_project_uuid}/",
                offering=f"{client._base_url}/api/marketplace-public-offerings/{self.order.offering.backend_id}/",
                plan=self.order.plan
                and f"{client._base_url}/api/marketplace-public-offerings/{self.order.offering.backend_id}/plans/{self.order.plan.backend_id}/"
                or UNSET,
                attributes=converted_attributes,  # type: ignore
                limits=OrderCreateRequestLimits.from_dict(self.order.limits),
                callback_url=build_callback_url(self.order),
                accepting_terms_of_service=True,
            ),
        )
        # NB: As a backend_id of local Order, uuid of a remote Order is used
        if response:
            self.order.backend_id = response.uuid.hex
            self.order.save()

        if settings.WALDUR_AUTH_SOCIAL["ENABLE_EDUTEAMS_SYNC"]:  # type: ignore
            utils.push_project_users(
                self.order.offering,
                self.order.project,
                remote_project_uuid,
            )

        transaction.on_commit(
            lambda: OrderStatePullTask().apply_async(
                args=[serialize_instance(self.order)],
                kwargs={},
                max_retries=MAX_RETRIES,
            )
        )


class RemoteUpdateResourceProcessor(processors.BasicUpdateResourceProcessor):
    def update_limits_process(self, user: User):
        client = utils.get_client_for_offering(self.order.offering)
        # Check if limits are already set on the remote side
        try:
            remote_resource = marketplace_resources_retrieve.sync(
                client=client, uuid=UUID(self.order.resource.backend_id)
            )
            remote_limits = (
                remote_resource.limits.to_dict() if remote_resource.limits else {}
            )

            if remote_limits == self.order.limits:
                message = f"Remote limits already match requested limits for order {self.order.uuid}. Remote: {remote_limits}, Requested: {self.order.limits}"
                logger.info(message)
                self.order.output = (
                    "Remote limits already match requested limits. No update needed."
                )
                self.order.save(update_fields=["output"])
                return True
        except Exception as e:
            logger.warning(
                f"Could not check remote limits, proceeding with update: {e}"
            )

        try:
            response = marketplace_resources_update_limits.sync(
                client=client,
                uuid=UUID(self.order.resource.backend_id),
                body=ResourceUpdateLimitsRequest(
                    limits=ResourceUpdateLimitsRequestLimits.from_dict(
                        self.order.limits
                    ),
                ),
            )
            if response:
                self.order.backend_id = response.order_uuid.hex
                self.order.save(update_fields=["backend_id"])
        except UnexpectedStatus as e:
            # Check if the error is because the limits are the same and return True if it is
            if e.status_code == 400 and "Impossible to create update orders" in str(e):
                message = f"Remote API rejected update as no change needed for order {self.order.uuid}. Requested limits: {self.order.limits}"
                logger.info(message)
                self.order.output = (
                    "Remote limits already match requested limits. No update needed."
                )
                self.order.save(update_fields=["output"])
                return True
            raise

        transaction.on_commit(
            lambda: OrderStatePullTask().apply_async(
                args=[serialize_instance(self.order)],
                kwargs={},
                max_retries=MAX_RETRIES,
            )
        )

        return False


class RemoteDeleteResourceProcessor(processors.BasicDeleteResourceProcessor):
    def send_request(self, user, resource: models.Resource):
        # Resource is switched to terminated state by caller method
        if not resource.backend_id:
            return True

        # If terminate order already exists in the remote side,
        # it should be imported and local order is switched to erred.
        imported_orders = utils.import_resource_orders(resource)
        if imported_orders:
            utils.pull_resource_state(resource)
        if any(item.type == OrderTypes.TERMINATE for item in imported_orders):
            self.order.set_state_erred()
            self.order.error_message = "Another order exists already."
            self.order.save()
            return False

        client = utils.get_client_for_offering(self.order.offering)
        response = marketplace_resources_terminate.sync(
            client=client,
            uuid=UUID(self.order.resource.backend_id),
            body=ResourceTerminateRequest(),
        )
        if response:
            self.order.backend_id = response.order_uuid.hex
            self.order.save(update_fields=["backend_id"])

        transaction.on_commit(
            lambda: OrderStatePullTask().apply_async(
                args=[serialize_instance(self.order)],
                kwargs={},
                max_retries=MAX_RETRIES,
            )
        )

        return False
