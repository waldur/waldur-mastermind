import collections
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

import requests
from celery.app import shared_task
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.utils import dateparse, timezone
from httpx import TransportError
from rest_framework import exceptions as rf_exceptions
from rest_framework import status
from waldur_api_client.api.maintenance_announcements import (
    maintenance_announcements_list,
)
from waldur_api_client.api.marketplace_component_usages import (
    marketplace_component_usages_list,
)
from waldur_api_client.api.marketplace_component_user_usages import (
    marketplace_component_user_usages_list,
)
from waldur_api_client.api.marketplace_offering_terms_of_service import (
    marketplace_offering_terms_of_service_list,
)
from waldur_api_client.api.marketplace_offering_users import (
    marketplace_offering_users_list,
)
from waldur_api_client.api.marketplace_orders import marketplace_orders_retrieve
from waldur_api_client.api.marketplace_public_offerings import (
    marketplace_public_offerings_retrieve,
)
from waldur_api_client.api.marketplace_resources import (
    marketplace_resources_retrieve,
)
from waldur_api_client.api.marketplace_robot_accounts import (
    marketplace_robot_accounts_list,
)
from waldur_api_client.api.projects import (
    projects_add_user,
    projects_delete_user,
    projects_destroy,
    projects_list,
    projects_list_users_list,
    projects_update_user,
)
from waldur_api_client.api.remote_eduteams import (
    remote_eduteams as get_remote_eduteams_user,
)
from waldur_api_client.client import AuthenticatedClient
from waldur_api_client.errors import UnexpectedStatus
from waldur_api_client.models.base_public_plan import BasePublicPlan
from waldur_api_client.models.maintenance_announcement import (
    MaintenanceAnnouncement as RemoteMaintenanceAnnouncement,
)
from waldur_api_client.models.maintenance_announcement_state_enum import (
    MaintenanceAnnouncementStateEnum,
)
from waldur_api_client.models.offering_component import OfferingComponent
from waldur_api_client.models.public_offering_details import PublicOfferingDetails
from waldur_api_client.models.remote_eduteams_request_request import (
    RemoteEduteamsRequestRequest as RemoteEduteamsRequest,
)
from waldur_api_client.models.robot_account_states import (
    RobotAccountStates as ApiRobotAccountStates,
)
from waldur_api_client.models.user_role_create_request import UserRoleCreateRequest
from waldur_api_client.models.user_role_delete_request import UserRoleDeleteRequest
from waldur_api_client.models.user_role_update_request import UserRoleUpdateRequest

from waldur_core.core.client import ClientValidationError, get_waldur_client
from waldur_core.core.enums import ReviewStates
from waldur_core.core.utils import (
    broadcast_mail,
    deserialize_instance,
    format_homeport_link,
    month_start,
    serialize_instance,
)
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import get_users_with_permission
from waldur_core.structure import models as structure_models
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_core.structure.tasks import BackgroundListPullTask, BackgroundPullTask
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.callbacks import sync_order_state
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    MaintenanceState,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
    RobotAccountStates,
)
from waldur_mastermind.marketplace.utils import get_or_create_plan_period
from waldur_mastermind.marketplace_remote import models as remote_models
from waldur_mastermind.marketplace_remote import (
    utils,
    utils_sync_remote_offerings,
)
from waldur_mastermind.marketplace_remote.constants import (
    OFFERING_COMPONENT_FIELDS,
    OFFERING_FIELDS,
    PLAN_FIELDS,
    RESOURCE_FIELDS,
)
from waldur_mastermind.marketplace_remote.exceptions import RemoteWaldurError
from waldur_mastermind.marketplace_remote.utils import (
    get_client_for_offering,
    pull_fields,
    sync_project_permission,
)

logger = logging.getLogger(__name__)

# For logging purposes only
ORDER_STATES_MAP = {key: val for key, val in OrderStates.CHOICES}
LOGICAL_LOCAL_ORDER_STATES_MAP = {
    "pending-project": None,
    "pending-consumer": OrderStates.EXECUTING,
    "pending-provider": OrderStates.EXECUTING,
    "executing": OrderStates.EXECUTING,
    "done": OrderStates.DONE,
    "erred": OrderStates.ERRED,
    "canceled": OrderStates.CANCELED,
    "rejected": OrderStates.CANCELED,  # If a remote order is rejected, the local one should switch from "executing" to "canceled"
}

DEFAULT_TOVERSION = "1.0"


class OfferingPullTask(BackgroundPullTask):
    def pull(self, local_offering: models.Offering):
        if not local_offering.backend_id:
            logger.warning(
                "Skipping pull for offering %s because its backend_id is empty.",
                local_offering,
            )
            return
        try:
            client = get_client_for_offering(local_offering)
            remote_offering = marketplace_public_offerings_retrieve.sync(
                client=client, uuid=local_offering.backend_id
            )
            pull_fields(OFFERING_FIELDS, local_offering, remote_offering.to_dict())
            utils.import_offering_thumbnail(local_offering, remote_offering.thumbnail)
            self.sync_offering_components(local_offering, remote_offering.components)
            self.sync_plans(local_offering, remote_offering.plans)
            self.sync_access_endpoints(local_offering, remote_offering)
            self.sync_terms_of_service(local_offering, remote_offering, client)
        except UnexpectedStatus as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                if local_offering.state == OfferingStates.ACTIVE:
                    local_offering.archive()
                    local_offering.save(update_fields=["state"])
                    logger.warning(exc)
                if local_offering.state == OfferingStates.ARCHIVED:
                    logger.debug("Offering %s is archived: ", local_offering)
            else:
                logger.exception(exc)

    def sync_terms_of_service(
        self,
        local_offering: models.Offering,
        remote_offering_data: PublicOfferingDetails,
        client: AuthenticatedClient,
    ):
        """Backwards compatibility for old-style ToS of remote offerings."""
        terms_of_service = getattr(remote_offering_data, "terms_of_service", "") or ""
        terms_of_service_link = (
            getattr(remote_offering_data, "terms_of_service_link", "") or ""
        )

        # New API client will automatically move terms of service to additional_properties since they are not in the expected schema
        if not terms_of_service and not terms_of_service_link:
            additional_props = getattr(
                remote_offering_data, "additional_properties", {}
            )
            terms_of_service = additional_props.get("terms_of_service", "") or ""
            terms_of_service_link = (
                additional_props.get("terms_of_service_link", "") or ""
            )

            # If still not found, check nested additional_properties
            if not terms_of_service and not terms_of_service_link:
                nested_props = additional_props.get("additional_properties", {})
                terms_of_service = nested_props.get("terms_of_service", "") or ""
                terms_of_service_link = (
                    nested_props.get("terms_of_service_link", "") or ""
                )

        if terms_of_service or terms_of_service_link:
            models.OfferingTermsOfService.objects.update_or_create(
                offering=local_offering,
                version=DEFAULT_TOVERSION,
                defaults={
                    "terms_of_service": terms_of_service,
                    "terms_of_service_link": terms_of_service_link,
                    "is_active": True,
                },
            )
        else:
            try:
                remote_terms_of_service_list = (
                    marketplace_offering_terms_of_service_list.sync_all(
                        client=client,
                        offering_uuid=remote_offering_data.uuid,
                    )
                )
                if not remote_terms_of_service_list:
                    logger.info(
                        "No terms of service found for offering %s", local_offering
                    )
                    models.OfferingTermsOfService.objects.filter(
                        offering=local_offering
                    ).delete()
                    return
                remote_terms_of_service = remote_terms_of_service_list[0]
                local_terms_of_service: models.OfferingTermsOfService | None = (
                    models.OfferingTermsOfService.objects.filter(
                        offering=local_offering,
                        version=remote_terms_of_service.version or DEFAULT_TOVERSION,
                    ).first()
                )
                fields = {
                    "terms_of_service": remote_terms_of_service.terms_of_service or "",
                    "terms_of_service_link": remote_terms_of_service.terms_of_service_link
                    or "",
                    "version": remote_terms_of_service.version or DEFAULT_TOVERSION,
                    "is_active": remote_terms_of_service.is_active,
                    "requires_reconsent": remote_terms_of_service.requires_reconsent,
                }
                if local_terms_of_service:
                    for field, value in fields.items():
                        setattr(local_terms_of_service, field, value)
                    local_terms_of_service.save()
                    logger.info(
                        "Updated existing ToS for offering %s (version %s)",
                        local_offering,
                        remote_terms_of_service.version or DEFAULT_TOVERSION,
                    )
                else:
                    models.OfferingTermsOfService.objects.create(
                        offering=local_offering, **fields
                    )
                    logger.info(
                        "Created new ToS for offering %s (version %s)",
                        local_offering,
                        remote_terms_of_service.version or DEFAULT_TOVERSION,
                    )
            except UnexpectedStatus as exc:
                logger.warning(
                    "Failed to sync terms of service for offering %s: %s",
                    local_offering,
                    exc,
                )

    def sync_access_endpoints(
        self, local_offering: models.Offering, remote_offering: PublicOfferingDetails
    ):
        if not remote_offering.endpoints:
            return
        remote_endpoints = remote_offering.endpoints
        local_endpoints = local_offering.endpoints.all()
        remote_endpoints_map = {item.url: item for item in remote_endpoints}
        local_endpoint_urls = {item.url for item in local_endpoints}

        new_urls = set(remote_endpoints_map.keys()) - local_endpoint_urls
        stale_urls = local_endpoint_urls - set(remote_endpoints_map.keys())
        existing_urls = local_endpoint_urls & set(remote_endpoints_map.keys())

        if stale_urls:
            local_offering.endpoints.filter(url__in=stale_urls).delete()
            logger.info(
                "Endpoints %s of offering %s have been deleted",
                stale_urls,
                local_offering,
            )

        for new_url in new_urls:
            models.OfferingAccessEndpoint.objects.create(
                url=new_url,
                name=remote_endpoints_map[new_url].name,
                offering=local_offering,
            )

        for existing_url in existing_urls:
            endpoint: models.OfferingAccessEndpoint = local_offering.endpoints.get(
                url=existing_url
            )
            if endpoint.name != remote_endpoints_map[existing_url].name:
                endpoint.name = remote_endpoints_map[existing_url].name
                endpoint.save(update_fields=["name"])

    def sync_offering_components(
        self,
        local_offering: models.Offering,
        remote_components: list[OfferingComponent],
    ):
        local_components = local_offering.components.all()
        remote_component_types_map = {item.type_: item for item in remote_components}
        local_component_types = [item.type for item in local_components]

        new_component_types = set(remote_component_types_map.keys()) - set(
            local_component_types
        )
        stale_component_types = set(local_component_types) - set(
            remote_component_types_map.keys()
        )
        existing_component_types = set(local_component_types) & set(
            remote_component_types_map.keys()
        )
        if stale_component_types:
            local_offering.components.filter(type__in=stale_component_types).delete()
            logger.info(
                "Components %s of offering %s have been deleted",
                stale_component_types,
                local_offering,
            )

        utils.import_offering_components(
            local_offering,
            [
                comp
                for comp_type, comp in remote_component_types_map.items()
                if comp_type in new_component_types
            ],
        )

        for existing_component_type in existing_component_types:
            remote_component = remote_component_types_map[existing_component_type]
            local_component: models.OfferingComponent = local_offering.components.get(
                type=existing_component_type
            )
            pull_fields(
                OFFERING_COMPONENT_FIELDS, local_component, remote_component.to_dict()
            )
            logger.info(
                "Component %s for offering %s has been updated",
                existing_component_type,
                local_offering,
            )

    def sync_plans(
        self, local_offering: models.Offering, remote_plans: list[BasePublicPlan]
    ):
        """
        Sync plans for an existing offering
        """
        local_plans = models.Plan.objects.filter(offering=local_offering)

        local_plan_uuids = [item.backend_id for item in local_plans]
        remote_plans_map = {item.uuid.hex: item for item in remote_plans}

        new_plans = set(remote_plans_map.keys()) - set(local_plan_uuids)
        stale_plans = set(local_plan_uuids) - set(remote_plans_map.keys())
        existing_plans = set(local_plan_uuids) & set(remote_plans_map.keys())

        for stale_plan in local_offering.plans.filter(backend_id__in=stale_plans):
            stale_plan.archived = True
            stale_plan.save()
            logger.info(
                "Plan %s of offering %s has been archived",
                stale_plan,
                local_offering,
            )

        local_components_map = {
            item.type: item for item in local_offering.components.all()
        }
        new_remote_plans = [item for item in remote_plans if item.uuid.hex in new_plans]
        utils.import_plans(local_offering, new_remote_plans, local_components_map)

        for existing_plan_backend_id in existing_plans:
            remote_plan = remote_plans_map[existing_plan_backend_id]
            local_plan: models.Plan = local_offering.plans.get(
                backend_id=existing_plan_backend_id
            )
            updated_fields = pull_fields(PLAN_FIELDS, local_plan, remote_plan.to_dict())

            self.sync_plan_components(local_plan, remote_plan)

            if updated_fields:
                logger.info(
                    "Plan %s for offering %s has been updated",
                    local_plan.name,
                    local_offering,
                )

    def sync_plan_components(
        self, local_plan: models.Plan, remote_plan: BasePublicPlan
    ):
        """
        Sync plan componets for an existing plan
        This method skips check of stale plan components, because it assumes they have been already removed in `sync_components` method
        """
        local_offering = local_plan.offering
        local_offering_components = local_offering.components
        local_plan_components = set(
            local_plan.components.all().values_list("component__type", flat=True)
        )
        remote_prices = remote_plan.prices.to_dict()
        remote_quotas = remote_plan.quotas.to_dict()
        remote_plan_components = set(remote_prices.keys()) | set(remote_quotas.keys())

        new_plan_components = remote_plan_components - local_plan_components

        existing_plan_components = local_plan_components & remote_plan_components

        for component_type in new_plan_components:
            plan_component = models.PlanComponent.objects.create(
                plan=local_plan,
                component=local_offering_components.get(type=component_type),
                price=remote_prices[component_type],
                amount=remote_quotas[component_type],
            )
            logger.info(
                "Plan component %s of offering %s has been created",
                plan_component,
                local_plan.offering,
            )

        for existing_plan_component in existing_plan_components:
            local_component: models.OfferingComponent = local_offering_components.get(
                type=existing_plan_component
            )
            local_plan_component: models.PlanComponent = local_plan.components.get(
                component=local_component
            )
            changed_fields = pull_fields(
                ["price", "amount"],
                local_plan_component,
                {
                    "price": remote_prices[existing_plan_component],
                    "amount": remote_quotas[existing_plan_component],
                },
            )

            if changed_fields:
                logger.info(
                    "Plan component %s of offering %s has been updated",
                    existing_plan_component,
                    local_offering,
                )


class OfferingListPullTask(BackgroundListPullTask):
    """Pull and synchronize remote marketplace offerings.

    This task synchronizes offerings from remote Waldur instances, updating
    local offering data including components, plans, and access endpoints.
    Runs every 60 minutes via celery beat.
    """

    name = "waldur_mastermind.marketplace_remote.pull_offerings"
    pull_task = OfferingPullTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(
            type=REMOTE_OFFERING, secret_options__has_keys=["api_url", "token"]
        ).exclude(backend_id="")


class OfferingUserPullTask(BackgroundPullTask):
    def pull(self, local_offering: models.Offering):
        client = get_client_for_offering(local_offering)
        remote_offering_users = {
            remote_offering_user.user_username: remote_offering_user.username
            for remote_offering_user in marketplace_offering_users_list.sync_all(
                client=client, offering_uuid=[UUID(local_offering.backend_id)]
            )
        }
        # Build lookup dicts upfront to avoid N+1 queries
        local_offering_user_objects = {
            offering_user.user.username: offering_user
            for offering_user in models.OfferingUser.objects.filter(
                offering=local_offering
            ).select_related("user")
        }
        local_offering_users = {
            username: offering_user.username
            for username, offering_user in local_offering_user_objects.items()
        }
        usernames = set(remote_offering_users.keys()) | set(local_offering_users.keys())
        user_map = {
            user.username: user
            for user in models.User.objects.filter(username__in=usernames)
        }

        missing = set(remote_offering_users.keys()) - set(local_offering_users.keys())
        for local_username in missing:
            if local_username not in user_map:
                logger.debug(
                    "Skipping missing offering user synchronization because user "
                    "with username %s is not available in the local database.",
                    local_username,
                )
                continue
            user = user_map[local_username]
            models.OfferingUser.objects.create(
                user=user,
                offering=local_offering,
                username=remote_offering_users[local_username],
            )

        stale = set(local_offering_users.keys()) - set(remote_offering_users.keys())
        for local_username in stale:
            if local_username not in user_map:
                try:
                    user = models.User.all_objects.get(username=local_username)
                    if not user.is_active:
                        logger.info(
                            "Skipping offering user synchronization for deactivated user %s",
                            local_username,
                        )
                        continue
                except models.User.DoesNotExist:
                    logger.debug(
                        "Skipping missing offering user synchronization because user "
                        "with username %s does not exist.",
                        local_username,
                    )
                    continue
            # O(1) lookup instead of database query
            offering_user = local_offering_user_objects[local_username]
            offering_user.delete()

        common = set(local_offering_users.keys()) & set(remote_offering_users.keys())
        for local_username in common:
            remote_username = remote_offering_users[local_username]
            if local_offering_users[local_username] == remote_username:
                continue
            # O(1) lookup instead of database query
            offering_user = local_offering_user_objects[local_username]
            offering_user.username = remote_username
            offering_user.save(update_fields=["username"])


class OfferingUserListPullTask(BackgroundListPullTask):
    """Pull and synchronize remote marketplace offering users.

    This task synchronizes user associations with marketplace offerings from
    remote Waldur instances, ensuring local user mappings are up to date.
    Runs every 60 minutes via celery beat.
    """

    name = "waldur_mastermind.marketplace_remote.pull_offering_users"
    pull_task = OfferingUserPullTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(
            type=REMOTE_OFFERING, secret_options__has_keys=["api_url", "token"]
        ).filter(
            Q(plugin_options__service_provider_can_create_offering_user__isnull=True)
            | Q(plugin_options__service_provider_can_create_offering_user=False)
        )


class ResourcePullTask(BackgroundPullTask):
    def pull(self, local_resource: models.Resource):
        client = get_client_for_offering(local_resource.offering)
        remote_resource = marketplace_resources_retrieve.sync(
            client=client, uuid=local_resource.backend_id
        )
        pull_fields(RESOURCE_FIELDS, local_resource, remote_resource.to_dict())
        if local_resource.effective_id != remote_resource.backend_id:
            local_resource.effective_id = remote_resource.backend_id
            local_resource.save(update_fields=["effective_id"])
        utils.import_resource_orders(local_resource)
        remote_state = utils.parse_resource_state(remote_resource.state.value)
        if remote_state != local_resource.state:
            local_resource.state = remote_state
            local_resource.save(update_fields=["state"])


class ResourceListPullTask(BackgroundListPullTask):
    """Pull and synchronize remote marketplace resources.

    This task synchronizes resource data from remote Waldur instances,
    updating local resource states and importing remote orders when needed.
    Runs every 60 minutes via celery beat.
    """

    name = "waldur_mastermind.marketplace_remote.pull_resources"
    pull_task = ResourcePullTask

    def get_pulled_objects(self):
        return models.Resource.objects.filter(offering__type=REMOTE_OFFERING).exclude(
            backend_id=""
        )


@shared_task(
    name="waldur_mastermind.marketplace_remote.reconcile_resource_end_dates",
)
def reconcile_resource_end_dates():
    resources = (
        models.Resource.objects.filter(offering__type=REMOTE_OFFERING)
        .exclude(backend_id="")
        .exclude(
            state__in=[
                ResourceStates.CREATING,
                ResourceStates.TERMINATING,
                ResourceStates.TERMINATED,
            ]
        )
    )
    for resource in resources:
        utils.reconcile_resource_end_date(resource)


@shared_task(
    name="waldur_mastermind.marketplace_remote.notify_resource_end_date_pulled_from_remote"
)
def notify_resource_end_date_pulled_from_remote(
    resource_uuid, old_end_date, new_end_date, remote_events=None
):
    """Send notification when a resource's end date is pulled from remote.

    This happens when the local end_date was in the past and the remote
    has a valid future date, so we sync from remote instead of pushing.
    """
    from waldur_core.logging import event_logger

    try:
        resource = models.Resource.objects.get(uuid=resource_uuid)
    except models.Resource.DoesNotExist:
        logger.warning("Resource %s not found for end date notification", resource_uuid)
        return

    # Emit audit log event
    event_logger.emit(
        "End date of marketplace resource %(resource_name)s has been automatically "
        "updated from %(old_end_date)s to %(new_end_date)s "
        "(synced from remote allocation)."
        % {
            "resource_name": resource.name,
            "old_end_date": old_end_date,
            "new_end_date": new_end_date,
        },
        event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_END_DATE_SUCCEEDED,
        event_context={"resource": resource},
        scopes=[resource, resource.project, resource.project.customer],
    )

    # Determine recipients: users with APPROVE_ORDER permission on the project/customer
    recipients = set()
    for scope in [resource.project, resource.project.customer]:
        users = get_users_with_permission(scope, PermissionEnum.APPROVE_ORDER)
        for user in users.exclude(email="").exclude(notifications_enabled=False):
            recipients.add(user.email)

    if not recipients:
        logger.info(
            "No recipients found for end date sync notification for resource %s",
            resource,
        )
        return

    resource_url = format_homeport_link(
        "project-resource-details/{resource_uuid}/",
        project_uuid=resource.project.uuid.hex,
        resource_uuid=resource.uuid.hex,
    )

    context = {
        "resource": resource,
        "old_end_date": old_end_date,
        "new_end_date": new_end_date,
        "resource_url": resource_url,
        "remote_events": remote_events or [],
    }

    broadcast_mail(
        "marketplace_remote",
        "resource_end_date_pulled_from_remote",
        context,
        list(recipients),
    )


@shared_task
def pull_offering_resources(serialized_offering):
    """Pull resources for a specific offering.

    This task pulls all resources associated with a specific offering,
    triggering individual resource pull tasks for each resource.
    Used for targeted synchronization of a single offering's resources.
    """
    offering = deserialize_instance(serialized_offering)
    resources = models.Resource.objects.filter(offering=offering).exclude(backend_id="")
    for resource in resources:
        ResourcePullTask().delay(serialize_instance(resource))


class OrderPullTask(BackgroundPullTask):
    def pull(self, local_order: models.Order):
        if not local_order.backend_id:
            return
        client = get_client_for_offering(local_order.offering)
        remote_order = marketplace_orders_retrieve.sync(
            client=client, uuid=local_order.backend_id
        )

        correct_local_order_state = LOGICAL_LOCAL_ORDER_STATES_MAP.get(
            remote_order.state.value
        )
        if correct_local_order_state is None:
            message = f"The order in remote Waldur has unexpected state {remote_order.state.value}."
            logger.error(message)
            raise RemoteWaldurError(message)

        if local_order.state != correct_local_order_state:
            logger.info(
                "Local order state %s is different from remote order state %s. Setting local order state to %s.",
                local_order.get_state_display(),
                remote_order.state.value,
                ORDER_STATES_MAP[correct_local_order_state],
            )
            sync_order_state(local_order, correct_local_order_state)

        local_resource = local_order.resource

        backend_id = remote_order.marketplace_resource_uuid
        if backend_id and local_resource.backend_id != backend_id:
            local_resource.backend_id = backend_id
            local_resource.save(update_fields=["backend_id"])

        pull_fields(("error_message",), local_order, remote_order.to_dict())

    def set_instance_erred(self, instance: models.Order, error_message):
        """Mark order as erred and save error message"""
        instance.set_state_erred()
        instance.error_message = error_message
        instance.save(update_fields=["state", "error_message"])


class OrderStatePullTask(OrderPullTask):
    def pull(self, local_order: models.Order):
        super().pull(local_order)
        local_order.refresh_from_db()
        if local_order.state not in OrderStates.TERMINAL_STATES:
            self.retry()


class OrderListPullTask(BackgroundListPullTask):
    """Pull and synchronize remote marketplace orders.

    This task synchronizes order states from remote Waldur instances,
    updating local order states and associated resource backend IDs.
    Only processes non-terminal orders. Runs every 60 minutes via celery beat.
    """

    name = "waldur_mastermind.marketplace_remote.pull_orders"
    pull_task = OrderPullTask

    def get_pulled_objects(self):
        return (
            models.Order.objects.filter(offering__type=REMOTE_OFFERING)
            .exclude(state__in=OrderStates.TERMINAL_STATES)
            .exclude(backend_id="")
        )


class ErredOrderPullTask(OrderPullTask):
    """Synchronises state for an erred local order and a linked resource.

    If a local order with UPDATE or TERMINATE type has a link to a remote order with a valid state,
    the state of local objects are adjusted accordingly.
    Valid states for a remote order: PENDING_CONSUMER, PENDING_PROVIDER and EXECUTING.
    """

    def pull(self, local_order: models.Order):
        if not local_order.backend_id:
            return
        client = get_client_for_offering(local_order.offering)
        remote_order = marketplace_orders_retrieve.sync(
            client=client, uuid=local_order.backend_id
        )
        local_resource: models.Resource = local_order.resource

        correct_local_order_state = LOGICAL_LOCAL_ORDER_STATES_MAP.get(
            remote_order.state.value
        )
        if correct_local_order_state is None:
            message = f"The order in remote Waldur has unexpected state {remote_order.state.value}."
            logger.error(message)
            raise RemoteWaldurError(message)

        if (
            local_order.state != correct_local_order_state
            and correct_local_order_state == OrderStates.EXECUTING
        ):
            logger.info(
                "Erred order %s: remote state is %s, updating local one.",
                local_order,
                remote_order.state.value,
            )
            local_order.state = correct_local_order_state
            local_order.save(update_fields=["state"])

            if local_order.type == OrderTypes.UPDATE:
                local_resource.set_state_updating()
            if local_order.type == OrderTypes.TERMINATE:
                local_resource.set_state_terminating()

            local_resource.save(update_fields=["state"])

        backend_id = remote_order.marketplace_resource_uuid
        if backend_id and local_resource.backend_id != backend_id:
            local_resource.backend_id = backend_id
            local_resource.save(update_fields=["backend_id"])

        pull_fields(["error_message"], local_order, remote_order.to_dict())


class ErredOrderListPullTask(BackgroundListPullTask):
    """Pull and synchronize erred remote marketplace orders.

    This task specifically handles erred local orders that may have been
    resolved in remote Waldur instances. It synchronizes UPDATE and TERMINATE
    order states and adjusts local resource states accordingly.
    Runs daily via celery beat.
    """

    name = "waldur_mastermind.marketplace_remote.pull_erred_orders"
    pull_task = ErredOrderPullTask

    def get_pulled_objects(self):
        return (
            models.Order.objects.filter(offering__type=REMOTE_OFFERING)
            .exclude(backend_id="")
            .filter(
                state=OrderStates.ERRED,
                type__in=[OrderTypes.UPDATE, OrderTypes.TERMINATE],
                created__month=timezone.now().month,
            )
        )


@shared_task
def pull_offering_orders(serialized_offering):
    """Pull orders for a specific offering.

    This task pulls all non-terminal orders associated with a specific offering,
    triggering individual order pull tasks for each order.
    Used for targeted synchronization of a single offering's orders.
    """
    offering = deserialize_instance(serialized_offering)
    orders = (
        models.Order.objects.filter(offering=offering)
        .exclude(state__in=OrderStates.TERMINAL_STATES)
        .exclude(backend_id="")
    )
    for order in orders:
        OrderPullTask().delay(serialize_instance(order))


class UsagePullTask(BackgroundPullTask):
    def run(self, serialized_instance, **kwargs):
        instance = deserialize_instance(serialized_instance)
        try:
            self.pull(instance, **kwargs)
        except ServiceBackendError as e:
            self.on_pull_fail(instance, e)
        else:
            self.on_pull_success(instance)

    def pull(self, local_resource: models.Resource, from_creation_date=False):
        """Pull resource usage either from 4 month ago or since resource creation date.

        Optimized to fetch all user usages upfront to avoid N+1 API calls.
        Previously, this method made N+1 API calls per resource (1 for component usages,
        N for user usages per component). Now it makes only 2 API calls total.
        """
        client = get_client_for_offering(local_resource.offering)
        today = datetime.today()
        if from_creation_date:
            start_date = month_start(local_resource.created)
        else:
            start_date = month_start(today - relativedelta(months=4))

        logger.info("Pulling resource %s usages from %s", local_resource, start_date)

        # Fetch remote component usages (1 API call)
        remote_usages = marketplace_component_usages_list.sync_all(
            client=client,
            resource_uuid=local_resource.backend_id,
            date_after=start_date.date(),
        )

        usage_count = len(remote_usages) if remote_usages else 0
        logger.info("Processing %d usages for resource %s", usage_count, local_resource)

        # Fetch ALL user usages for this resource upfront (1 API call)
        # This replaces N API calls with 1, significantly reducing remote API load
        all_user_usages = marketplace_component_user_usages_list.sync_all(
            client=client,
            resource_uuid=local_resource.backend_id,
            date_after=start_date.date(),
        )

        # Group user usages by (billing_period, component_type) for O(1) lookup
        user_usages_by_key = collections.defaultdict(list)
        for user_usage in all_user_usages or []:
            key = (user_usage.billing_period, user_usage.component_type)
            user_usages_by_key[key].append(user_usage)

        logger.info(
            "Fetched %d user usages for resource %s",
            len(all_user_usages) if all_user_usages else 0,
            local_resource,
        )

        # Pre-fetch offering components to avoid repeated DB queries
        offering_components = {
            oc.type: oc
            for oc in models.OfferingComponent.objects.filter(
                offering=local_resource.offering
            )
        }

        processed_count = 0
        for remote_usage in remote_usages:
            offering_component = offering_components.get(remote_usage.type_)
            if not offering_component:
                continue

            usage_date = remote_usage.date
            if usage_date < local_resource.created:
                logger.info(
                    f"Invalid component usage date detected for resource {local_resource.id}"
                )
                continue

            defaults = {
                "usage": remote_usage.usage,
                "description": remote_usage.description,
                "created": remote_usage.created,
                "date": usage_date,
                "recurring": remote_usage.recurring,
                "backend_id": remote_usage.uuid.hex,
            }
            plan_period = get_or_create_plan_period(local_resource, usage_date)
            component_usage, _ = models.ComponentUsage.objects.update_or_create(
                resource=local_resource,
                component=offering_component,
                plan_period=plan_period,
                billing_period=remote_usage.billing_period,
                defaults=defaults,
            )

            # Look up user usages from pre-fetched dict (O(1) instead of API call)
            key = (remote_usage.billing_period, remote_usage.type_)
            remote_user_usages = user_usages_by_key.get(key, [])
            if remote_user_usages:
                self._process_user_usages(
                    local_resource, component_usage, remote_user_usages
                )

            processed_count += 1
            if processed_count % 50 == 0:
                logger.info(
                    "Processed %d/%d usages for resource %s",
                    processed_count,
                    usage_count,
                    local_resource,
                )

        logger.info(
            "Completed pulling %d usages for resource %s",
            processed_count,
            local_resource,
        )

    def _process_user_usages(self, local_resource, component_usage, remote_user_usages):
        """Process user usages for a component usage."""
        for remote_user_usage in remote_user_usages:
            if not remote_user_usage.username:
                continue
            if not remote_user_usage.usage:
                usage = Decimal(0)
            else:
                usage = Decimal(remote_user_usage.usage)
            offering_user = models.OfferingUser.objects.filter(
                offering=local_resource.offering,
                username=remote_user_usage.username,
            ).first()
            models.ComponentUserUsage.objects.update_or_create(
                component_usage=component_usage,
                username=remote_user_usage.username,
                defaults={
                    "usage": usage,
                    "user": offering_user,
                },
            )


class UsageListPullTask(BackgroundListPullTask):
    """Pull and synchronize remote marketplace resource usage data.

    This task synchronizes component usage data from remote Waldur instances,
    including both regular usage and user-specific usage metrics.
    Pulls usage data from the last 4 months. Runs every 60 minutes via celery beat.
    """

    name = "waldur_mastermind.marketplace_remote.pull_usage"
    pull_task = UsagePullTask

    def get_pulled_objects(self):
        return models.Resource.objects.exclude(backend_id="").filter(
            offering__type=REMOTE_OFFERING
        )


@shared_task
def pull_offering_usage(serialized_offering):
    """Pull usage data for a specific offering.

    This task pulls usage data for all resources associated with a specific
    offering, starting from each resource's creation date.
    Used for targeted synchronization of a single offering's usage data.
    """
    offering = deserialize_instance(serialized_offering)
    resources = models.Resource.objects.exclude(backend_id="").filter(offering=offering)
    for resource in resources:
        UsagePullTask().delay(serialize_instance(resource), from_creation_date=True)


# Monkey-patch the API client's RobotAccountStates to handle different enum versions
# Some versions use IntEnum with integer values (VALUE_1=1, VALUE_2=2, etc.)
# Other versions use StrEnum with string values (OK="OK", CREATING="Creating", etc.)
_original_new = ApiRobotAccountStates.__new__
_is_int_enum = issubclass(ApiRobotAccountStates, int)

# Mapping from display strings to both integer and string enum values
_STATE_DISPLAY_TO_INT = {
    "Requested": 1,
    "Creating": 2,
    "OK": 3,
    "Requested deletion": 4,
    "Deleted": 5,
    "Error": 6,
}
_STATE_INT_TO_DISPLAY = {v: k for k, v in _STATE_DISPLAY_TO_INT.items()}


def _patched_new(cls, value):
    """Handle both IntEnum and StrEnum versions of RobotAccountStates.

    Fixes PUHURI-PORTALS-DC4: ValueError: 3 is not a valid RobotAccountStates
    """
    if _is_int_enum:
        # IntEnum version expects integer values
        if isinstance(value, str):
            if value in _STATE_DISPLAY_TO_INT:
                # Convert display string to integer
                value = _STATE_DISPLAY_TO_INT[value]
            elif value.isdigit():
                # Handle numeric strings like "3"
                value = int(value)
    else:
        # StrEnum version expects string values
        if isinstance(value, int):
            # Convert integer to display string
            if value in _STATE_INT_TO_DISPLAY:
                value = _STATE_INT_TO_DISPLAY[value]
        elif isinstance(value, str) and value.isdigit():
            # Handle numeric strings like "3" - convert to display string
            int_value = int(value)
            if int_value in _STATE_INT_TO_DISPLAY:
                value = _STATE_INT_TO_DISPLAY[int_value]
    return _original_new(cls, value)


ApiRobotAccountStates.__new__ = _patched_new


class ResourceRobotAccountPullTask(BackgroundPullTask):
    def pull(self, local_resource: models.Resource):
        client = get_client_for_offering(local_resource.offering)
        remote_accounts = marketplace_robot_accounts_list.sync_all(
            client=client, resource_uuid=local_resource.backend_id
        )
        local_accounts = models.RobotAccount.objects.filter(resource=local_resource)

        # Build lookup dict upfront to avoid N+1 queries
        local_accounts_by_backend_id = {
            item.backend_id: item for item in local_accounts
        }
        local_ids = set(local_accounts_by_backend_id.keys())
        remote_ids = {item.uuid.hex for item in remote_accounts}

        new_ids = remote_ids - local_ids
        stale_ids = local_ids - remote_ids
        existing_ids = local_ids & remote_ids

        if stale_ids:
            local_accounts.filter(backend_id__in=stale_ids).delete()
            logger.info(
                f"The following robot accounts for resource [uuid={local_resource.uuid}] have been deleted: {stale_ids}"
            )

        new_accounts = [
            account for account in remote_accounts if account.uuid.hex in new_ids
        ]
        for remote_account in new_accounts:
            robot_account = models.RobotAccount.objects.create(
                resource=local_resource,
                backend_id=remote_account.uuid.hex,
                type=remote_account.type_,
                username=remote_account.username,
                keys=remote_account.keys,
            )
            # Set state to OK
            robot_account.state = RobotAccountStates.OK
            robot_account.save()

        existing_accounts = [
            account for account in remote_accounts if account.uuid.hex in existing_ids
        ]
        for remote_account in existing_accounts:
            # O(1) lookup instead of database query
            local_account = local_accounts_by_backend_id[remote_account.uuid.hex]
            modified = set()
            if local_account.type != remote_account.type_:
                local_account.type = remote_account.type_
                modified.add("type")
            if local_account.username != remote_account.username:
                local_account.username = remote_account.username
                modified.add("username")
            if local_account.keys != remote_account.keys:
                local_account.keys = remote_account.keys
                modified.add("keys")
            if modified:
                local_account.save(update_fields=modified)


class ResourceRobotAccountListPullTask(BackgroundListPullTask):
    """Pull and synchronize remote marketplace resource robot accounts.

    This task synchronizes robot account data for marketplace resources from
    remote Waldur instances, including account types, usernames, and keys.
    Runs every 60 minutes via celery beat.
    """

    name = "waldur_mastermind.marketplace_remote.pull_robot_accounts"
    pull_task = ResourceRobotAccountPullTask

    def get_pulled_objects(self):
        return (
            models.Resource.objects.filter(offering__type=REMOTE_OFFERING)
            .exclude(state=ResourceStates.TERMINATED)
            .exclude(backend_id="")
        )


@shared_task
def pull_offering_robot_accounts(serialized_offering):
    """Pull robot accounts for a specific offering.

    This task pulls robot account data for all resources associated with a
    specific offering, excluding terminated resources.
    Used for targeted synchronization of a single offering's robot accounts.
    """
    offering = deserialize_instance(serialized_offering)
    resources = (
        models.Resource.objects.filter(offering=offering)
        .exclude(state=ResourceStates.TERMINATED)
        .exclude(backend_id="")
    )
    for resource in resources:
        ResourceRobotAccountPullTask().delay(serialize_instance(resource))


@shared_task(
    name="waldur_mastermind.marketplace_remote.update_remote_project_permissions"
)
def update_remote_project_permissions(
    serialized_project,
    serialized_user,
    role_name,
    grant=True,
    expiration_time=None,
):
    """Update project permissions in remote Waldur instances.

    This task grants or revokes a specific role for a user on a project
    in remote Waldur instances. Used to synchronize permission changes.

    Args:
        serialized_project: Serialized project instance
        serialized_user: Serialized user instance
        role_name: Name of the role to grant/revoke
        grant: Whether to grant (True) or revoke (False) the permission
        expiration_time: Optional expiration time for the permission
    """
    try:
        project = deserialize_instance(serialized_project)
        user = deserialize_instance(serialized_user)
    except ObjectDoesNotExist:
        utils.log_permission_sync_skip_reason(serialized_project, serialized_user)
        return

    new_expiration_time = (
        dateparse.parse_datetime(expiration_time)
        if expiration_time
        else expiration_time
    )
    sync_project_permission(grant, project, role_name, user, new_expiration_time)


@shared_task(
    name="waldur_mastermind.marketplace_remote.sync_remote_project_permissions"
)
def sync_remote_project_permissions():
    """Synchronize project permissions with remote Waldur instances.

    This task ensures that project permissions are synchronized between
    local and remote Waldur instances when eduTEAMS sync is enabled.
    It creates remote projects if needed and manages user role assignments.
    Runs every 6 hours via celery beat.

    Optimization: Caches remote user UUIDs per API endpoint to avoid
    redundant lookups when the same user appears across multiple projects/offerings.
    """
    if not settings.WALDUR_AUTH_SOCIAL["ENABLE_EDUTEAMS_SYNC"]:
        return

    # Cache remote user UUIDs by (api_url, username) to avoid redundant API calls
    # when same user appears in multiple projects/offerings on the same remote instance
    remote_user_uuid_cache: dict[tuple[str, str], str] = {}

    def get_cached_remote_user_uuid(client, api_url: str, username: str) -> str | None:
        """Get remote user UUID with caching to avoid redundant API calls."""
        cache_key = (api_url, username)
        if cache_key in remote_user_uuid_cache:
            return remote_user_uuid_cache[cache_key]

        try:
            remote_user_uuid = get_remote_eduteams_user.sync(
                client=client, body=RemoteEduteamsRequest(cuid=username)
            ).uuid.hex
            remote_user_uuid_cache[cache_key] = remote_user_uuid
            return remote_user_uuid
        except (UnexpectedStatus, TransportError):
            return None

    for project, offerings in utils.get_projects_with_remote_offerings().items():
        for offering in offerings:
            local_permissions = utils.collect_local_permissions(offering, project)
            client = utils.get_client_for_offering(offering)
            api_url = offering.secret_options.get("api_url", "")

            try:
                remote_project = utils.get_remote_project(offering, project, client)
                if not remote_project:
                    if not local_permissions:
                        logger.info(
                            f"Skipping remote project {project} synchronization in "
                            "offering {offering} because there are no users to be synced."
                        )
                    else:
                        remote_project = utils.create_remote_project(
                            offering, project, client
                        )
                        utils.push_project_users(
                            offering, project, remote_project.uuid.hex
                        )
                    continue
            except rf_exceptions.ValidationError as e:
                logger.warning(
                    f"Unable to fetch remote project {project} in offering {offering}: {e}"
                )
                continue
            except (UnexpectedStatus, TransportError) as e:
                logger.warning(
                    f"Unable to create remote project {project} in offering {offering}: {e}"
                )
                continue
            else:
                remote_project_uuid = remote_project.uuid.hex

            try:
                remote_permissions = projects_list_users_list.sync_all(
                    client=client, uuid=remote_project_uuid
                )
            except (UnexpectedStatus, TransportError) as e:
                logger.warning(
                    f"Unable to get project permissions for project {project} in offering {offering}: {e}"
                )
                continue

            remote_user_roles = collections.defaultdict[
                str, tuple[str, datetime, str]
            ]()
            for remote_permission in remote_permissions:
                remote_user_roles[remote_permission.user_username] = (
                    remote_permission.role_name,
                    remote_permission.expiration_time,
                    remote_permission.user_uuid.hex,
                )
                # Also cache UUIDs from remote permissions to avoid lookups for existing users
                remote_user_uuid_cache[(api_url, remote_permission.user_username)] = (
                    remote_permission.user_uuid.hex
                )

            for username, (new_role, new_expiration_time) in local_permissions.items():
                # Use cached lookup - avoids API call if user was seen before
                remote_user_uuid = get_cached_remote_user_uuid(
                    client, api_url, username
                )
                if not remote_user_uuid:
                    logger.warning(
                        f"Unable to fetch remote user {username} in offering {offering}"
                    )
                    continue

                if username not in remote_user_roles:
                    try:
                        projects_add_user.sync(
                            client=client,
                            uuid=remote_project_uuid,
                            body=UserRoleCreateRequest(
                                user=remote_user_uuid,
                                role=new_role,
                                expiration_time=new_expiration_time,
                            ),
                        )
                    except (UnexpectedStatus, TransportError) as e:
                        logger.warning(
                            f"Unable to create permission for user [{remote_user_uuid}] "
                            f"with role {new_role} (until {new_expiration_time}) "
                            f"and project [{remote_project_uuid}] in offering [{offering}]: {e}"
                        )
                    continue

                old_role, old_expiration_time, _ = remote_user_roles[username]

                if old_role != new_role:
                    try:
                        projects_delete_user.sync_detailed(
                            client=client,
                            uuid=remote_project_uuid,
                            body=UserRoleDeleteRequest(
                                user=remote_user_uuid, role=old_role
                            ),
                        )
                    except (UnexpectedStatus, TransportError) as e:
                        logger.warning(
                            f"Unable to remove permission for user [{remote_user_uuid}] with role {old_role} "
                            f"and project [{remote_project_uuid}] in offering [{offering}]: {e}"
                        )
                    try:
                        projects_add_user.sync(
                            client=client,
                            uuid=remote_project_uuid,
                            body=UserRoleCreateRequest(
                                user=remote_user_uuid,
                                role=new_role,
                                expiration_time=new_expiration_time,
                            ),
                        )
                    except (UnexpectedStatus, TransportError) as e:
                        logger.warning(
                            f"Unable to create permission for user [{remote_user_uuid}] "
                            f"with role {new_role} (until {new_expiration_time}) "
                            f"and project [{remote_project_uuid}] in offering [{offering}]: {e}"
                        )
                    continue

                if old_expiration_time != new_expiration_time:
                    try:
                        projects_update_user.sync(
                            client=client,
                            uuid=remote_project_uuid,
                            body=UserRoleUpdateRequest(
                                user=remote_user_uuid,
                                role=new_role,
                                expiration_time=new_expiration_time,
                            ),
                        )
                    except (UnexpectedStatus, TransportError) as e:
                        logger.warning(
                            f"Unable to update permission for user [{remote_user_uuid}] "
                            f"with role {old_role} (until {new_expiration_time}) "
                            f"and project [{remote_project_uuid}] in offering [{offering}]: {e}"
                        )

            stale_usernames = set(remote_user_roles.keys()) - set(
                local_permissions.keys()
            )
            for username in stale_usernames:
                role_name, _, remote_user_uuid = remote_user_roles[username]
                try:
                    projects_delete_user.sync_detailed(
                        client=client,
                        uuid=remote_project_uuid,
                        body=UserRoleDeleteRequest(
                            user=remote_user_uuid,
                            role=role_name,
                        ),
                    )
                except (UnexpectedStatus, TransportError) as e:
                    logger.warning(
                        f"Unable to remove permission [{role_name}] "
                        f"for user [{username}] in offering [{offering}]: {e}"
                    )


@shared_task
def sync_remote_project(serialized_request):
    """Synchronize a project update request with remote Waldur instances.

    This task processes a project update request and applies the changes
    to the corresponding remote project.

    Args:
        serialized_request: Serialized ProjectUpdateRequest instance
    """
    request = deserialize_instance(serialized_request)
    try:
        utils.update_remote_project(request)
    except (UnexpectedStatus, TransportError):
        logger.exception(
            f"Unable to update remote project {request.project} in offering {request.offering}"
        )


@shared_task
def delete_remote_project(serialized_project):
    """Delete a project from remote Waldur instances.

    This task deletes a project from all remote Waldur instances where
    it exists, based on the project's backend ID.

    Args:
        serialized_project: Serialized project instance
    """
    _, pk = serialized_project.split(":")
    try:
        local_project = structure_models.Project.objects.get(pk=pk)
    except structure_models.Project.DoesNotExist:
        # Project has been deleted via queryset method.
        return

    backend_id = utils.get_project_backend_id(local_project)
    offering_ids = (
        models.Resource.objects.filter(
            project=local_project,
            offering__type=REMOTE_OFFERING,
        )
        .values_list("offering_id", flat=True)
        .distinct()
    )
    offerings = models.Offering.objects.filter(pk__in=offering_ids)
    clients = {}

    for offering in offerings:
        if (
            "api_url" not in offering.secret_options.keys()
            or "token" not in offering.secret_options.keys()
        ):
            continue

        clients[offering.secret_options["api_url"]] = offering.secret_options["token"]

    for api_url, token in clients.items():
        try:
            client = get_waldur_client(api_url, token)
        except ClientValidationError:
            continue

        try:
            remote_projects = projects_list.sync(client=client, backend_id=backend_id)

            if len(remote_projects) != 1:
                continue

        except (UnexpectedStatus, TransportError) as e:
            logger.debug(
                f"Unable to get remote project (backend_id: {backend_id}): {e}"
            )
            continue

        try:
            remote_project = remote_projects[0]
            projects_destroy.sync_detailed(client=client, uuid=remote_project.uuid.hex)
        except (UnexpectedStatus, TransportError) as e:
            logger.debug(
                f"Unable to delete remote project {remote_project.uuid} (api_url: {api_url}): {e}"
            )
            continue


@shared_task
def clean_remote_projects():
    """Clean up stale projects from remote Waldur instances.

    This task removes projects from remote Waldur instances that correspond
    to locally removed projects, helping maintain consistency.
    """
    clients = {}
    projects_backend_ids = set(
        map(
            lambda project: utils.get_project_backend_id(project),
            structure_models.Project.objects.filter(is_removed=True),
        )
    )

    for offering in models.Offering.objects.filter(
        type=REMOTE_OFFERING,
        state__in=(
            OfferingStates.ACTIVE,
            OfferingStates.PAUSED,
            OfferingStates.UNAVAILABLE,
        ),
    ):
        if (
            "api_url" not in offering.secret_options.keys()
            or "token" not in offering.secret_options.keys()
        ):
            continue

        clients[offering.secret_options["api_url"]] = offering.secret_options["token"]

    for api_url, token in clients.items():
        client = get_waldur_client(api_url, token)

        try:
            remote_projects = projects_list.sync_all(client=client)
        except (UnexpectedStatus, TransportError) as e:
            logger.debug(f"Unable to get remote projects (api_url: {api_url}): {e}")
            continue

        for remote_project in remote_projects:
            if remote_project.backend_id in projects_backend_ids:
                try:
                    projects_destroy.sync_detailed(
                        client=client, uuid=remote_project.uuid.hex
                    )
                except (UnexpectedStatus, TransportError) as e:
                    logger.debug(
                        f"Unable to delete remote project "
                        f"(backend_id: {remote_project.backend_id}, api_url: {api_url}): {e}"
                    )
                    continue


@shared_task
def trigger_order_callback(serialized_order):
    """Trigger a callback for an order.

    This task sends a POST request to an order's callback URL,
    typically used to notify external systems about order completion.

    Args:
        serialized_order: Serialized order instance
    """
    order = deserialize_instance(serialized_order)
    requests.post(order.callback_url)


@shared_task(
    name="waldur_mastermind.marketplace_remote.notify_about_pending_project_update_requests"
)
def notify_about_pending_project_update_requests():
    """Notify about pending project update requests.

    This task sends email notifications to project owners about pending
    project update requests that have been waiting for more than a week.
    Runs weekly via celery beat.
    """
    week_ago = datetime.now() - timedelta(weeks=1)
    pending_project_update_requests = (
        remote_models.ProjectUpdateRequest.objects.filter(state=ReviewStates.PENDING)
        .order_by("project_id")
        .distinct("project_id")
        .filter(created__lte=week_ago)
    )

    for pending_project_update_request in pending_project_update_requests:
        mails = pending_project_update_request.project.customer.get_owner_mails()
        project_url = format_homeport_link(
            "projects/{project_uuid}/marketplace-project-update-requests/",
            project_uuid=pending_project_update_request.project.uuid.hex,
        )
        context = {
            "project_update_request": pending_project_update_request,
            "project_url": project_url,
        }
        broadcast_mail(
            "marketplace_remote",
            "notification_about_pending_project_updates",
            context,
            mails,
        )


@shared_task(
    name="waldur_mastermind.marketplace_remote.notify_about_project_details_update"
)
def notify_about_project_details_update(serialized_project_update):
    """Notify about project details update completion.

    This task sends email notifications to relevant users when a project
    update request has been processed, including details about what changed.

    Args:
        serialized_project_update: Serialized project update request instance
    """
    review_request = cast(
        remote_models.ProjectUpdateRequest,
        deserialize_instance(serialized_project_update),
    )

    context = {}
    if review_request.new_description:
        context["new_description"] = review_request.new_description
        context["old_description"] = review_request.old_description
    if review_request.new_name:
        context["new_name"] = review_request.new_name
        context["old_name"] = review_request.old_name
    if review_request.new_end_date:
        context["new_end_date"] = review_request.new_end_date
        context["old_end_date"] = review_request.old_end_date
    if review_request.new_oecd_fos_2007_code:
        context["new_oecd_fos_2007_code"] = review_request.new_oecd_fos_2007_code
        context["old_oecd_fos_2007_code"] = review_request.old_oecd_fos_2007_code
    if review_request.new_is_industry:
        context["new_is_industry"] = review_request.new_is_industry
        context["old_is_industry"] = review_request.new_is_industry

    context["reviewed_by"] = review_request.reviewed_by
    context["project_url"] = format_homeport_link(
        "projects/{project_uuid}/",
        project_uuid=review_request.project.uuid.hex,
    )
    mails = [
        review_request.reviewed_by,
        review_request.created_by,
    ]

    broadcast_mail(
        "marketplace_remote",
        "notification_about_project_details_update",
        context,
        mails,
    )


class RemoteProjectDataPushTask(BackgroundPullTask):
    def pull(self, instance: models.Offering):
        offering = instance
        project_ids = (
            models.Resource.objects.filter(offering=offering)
            .exclude(state=ResourceStates.TERMINATED)
            .values_list("project_id", flat=True)
            .distinct()
        )
        for project in structure_models.Project.objects.filter(id__in=project_ids):
            try:
                logger.info("Pushing project %s data to remote Waldur", project)
                request = remote_models.ProjectUpdateRequest(
                    project=project,
                    offering=offering,
                    new_name=project.name,
                    new_description=project.description,
                    new_end_date=project.end_date,
                    new_oecd_fos_2007_code=project.oecd_fos_2007_code,
                    new_is_industry=project.is_industry,
                )
                utils.update_remote_project(request)
            except (UnexpectedStatus, TransportError) as exc:
                logger.error("Unable to push project data: %s", exc)


class RemoteProjectDataListPushTask(BackgroundListPullTask):
    """Push project data to remote Waldur instances.

    This task pushes local project data (name, description, end date, etc.)
    to remote Waldur instances for projects that have marketplace resources.
    Runs daily via celery beat.
    """

    name = "waldur_mastermind.marketplace_remote.push_remote_project_data"
    pull_task = RemoteProjectDataPushTask

    def get_pulled_objects(self):
        return models.Offering.objects.filter(type=REMOTE_OFFERING)


class RemoteResourcePermissionsPushTask(BackgroundPullTask):
    def pull(self, instance: models.Offering):
        pass


@shared_task(name="waldur_mastermind.marketplace_remote.remote_offerings_sync")
def remote_offerings_sync() -> None:
    """Synchronize remote offerings based on RemoteSynchronisation configurations.

    This task processes active remote synchronization configurations,
    running synchronization for each configured remote marketplace.
    Runs daily via celery beat.
    """
    for sync in remote_models.RemoteSynchronisation.objects.filter(
        is_active=True,
    ).exclude(state=remote_models.RemoteSynchronisation.States.PROCESSING):
        utils_sync_remote_offerings.RemoteSynchronisationRunner(sync).run()


class MaintenanceAnnouncementPullTask(BackgroundPullTask):
    """Pull and synchronize remote maintenance announcements for a service provider.

    This task synchronizes maintenance announcements from remote Waldur instances,
    updating local maintenance data including affected offerings and impact levels.
    """

    def pull(self, service_provider: models.ServiceProvider):
        try:
            offering = models.Offering.objects.filter(
                customer=service_provider.customer,
                type=REMOTE_OFFERING,
                secret_options__has_keys=["api_url", "token"],
            ).first()
            if not offering:
                logger.info(
                    "No remote offerings found for service provider %s",
                    service_provider.customer.name,
                )
                return

            client = get_client_for_offering(offering)

            remote_maintenance_list = maintenance_announcements_list.sync_all(
                client=client,
                state=[
                    MaintenanceAnnouncementStateEnum.SCHEDULED,
                    MaintenanceAnnouncementStateEnum.IN_PROGRESS,
                ],
            )

            local_maintenance_list = models.MaintenanceAnnouncement.objects.filter(
                service_provider=service_provider
            )

            local_maintenance_map = {
                item.backend_id: item
                for item in local_maintenance_list
                if item.backend_id
            }

            remote_maintenance_map = {
                item.uuid.hex: item for item in remote_maintenance_list
            }

            local_maintenance_keys = set(local_maintenance_map.keys())
            remote_maintenance_keys = set(remote_maintenance_map.keys())

            new_maintenance_keys = remote_maintenance_keys - local_maintenance_keys
            stale_maintenance_keys = local_maintenance_keys - remote_maintenance_keys
            existing_maintenance_keys = local_maintenance_keys & remote_maintenance_keys
            announcements_to_update_or_create = (
                new_maintenance_keys | existing_maintenance_keys
            )
            if stale_maintenance_keys:
                stale_maintenances = [
                    local_maintenance_map[key] for key in stale_maintenance_keys
                ]
                ids = [m.id for m in stale_maintenances]
                models.MaintenanceAnnouncement.objects.filter(id__in=ids).delete()
                logger.info(
                    "Deleted stale maintenance announcements %s for service provider %s",
                    len(stale_maintenances),
                    service_provider.customer.name,
                )

            for maintenance_key in announcements_to_update_or_create:
                remote_maintenance = remote_maintenance_map[maintenance_key]
                self.update_or_create_local_maintenance(
                    service_provider, remote_maintenance
                )

        except UnexpectedStatus as exc:
            logger.exception(
                "Failed to sync maintenance announcements for service provider %s: %s",
                service_provider.customer.name,
                exc,
            )
            raise

    def update_or_create_local_maintenance(
        self, service_provider, remote_maintenance: RemoteMaintenanceAnnouncement
    ):
        """Create or update local maintenance announcement from remote data."""
        maintenance_state_map = {
            label: value for value, label in MaintenanceState.CHOICES
        }
        defaults = {
            "name": remote_maintenance.name,
            "message": remote_maintenance.message,
            "maintenance_type": remote_maintenance.maintenance_type.value
            if remote_maintenance.maintenance_type
            else None,
            "scheduled_start": remote_maintenance.scheduled_start,
            "scheduled_end": remote_maintenance.scheduled_end,
            "actual_start": remote_maintenance.actual_start,
            "actual_end": remote_maintenance.actual_end,
            "external_reference_url": remote_maintenance.external_reference_url,
            "state": maintenance_state_map.get(remote_maintenance.state.value),
        }

        local_maintenance, created = (
            models.MaintenanceAnnouncement.objects.update_or_create(
                service_provider=service_provider,
                backend_id=remote_maintenance.uuid.hex,
                defaults=defaults,
            )
        )

        self.sync_affected_maintenance_offerings(
            local_maintenance, remote_maintenance.affected_offerings
        )
        action = "Created" if created else "Updated"
        logger.info(
            "%s maintenance announcement '%s' for service provider %s",
            action,
            local_maintenance.name,
            service_provider.customer.name,
        )
        return local_maintenance

    def sync_affected_maintenance_offerings(
        self, local_maintenance, remote_affected_offerings
    ):
        """Sync affected offerings for a maintenance announcement."""
        if not remote_affected_offerings:
            return
        local_maintenance.affected_offerings.all().delete()

        # Collect all offering names from remote affected offerings
        offering_names = [
            remote_affected.offering_name
            for remote_affected in remote_affected_offerings
            if hasattr(remote_affected, "offering_name")
            and remote_affected.offering_name
        ]

        if not offering_names:
            logger.warning(
                "No valid offering names found for maintenance '%s'",
                local_maintenance.name,
            )
            return

        # Bulk fetch all local offerings in one query
        local_offerings_by_name = {
            offering.name: offering
            for offering in models.Offering.objects.filter(
                customer=local_maintenance.service_provider.customer,
                name__in=offering_names,
            )
        }

        # Build list of objects to create
        affected_offerings_to_create = []
        for remote_affected in remote_affected_offerings:
            if not (
                hasattr(remote_affected, "offering_name")
                and remote_affected.offering_name
            ):
                logger.warning(
                    "Cannot identify remote offering for maintenance '%s': no offering_name",
                    local_maintenance.name,
                )
                continue

            local_offering = local_offerings_by_name.get(remote_affected.offering_name)
            if not local_offering:
                logger.warning(
                    "Cannot sync affected offering '%s' for maintenance '%s': offering not found locally",
                    remote_affected.offering_name,
                    local_maintenance.name,
                )
                continue

            affected_offerings_to_create.append(
                models.MaintenanceAnnouncementOffering(
                    maintenance=local_maintenance,
                    offering=local_offering,
                    impact_level=getattr(remote_affected, "impact_level", 2),
                    impact_description=getattr(
                        remote_affected, "impact_description", ""
                    ),
                )
            )

        # Bulk create all affected offerings
        if affected_offerings_to_create:
            models.MaintenanceAnnouncementOffering.objects.bulk_create(
                affected_offerings_to_create
            )


class MaintenanceAnnouncementListPullTask(BackgroundListPullTask):
    pull_task = MaintenanceAnnouncementPullTask

    def get_pulled_objects(self):
        """Get service providers that have remote offerings to sync maintenance from."""
        remote_offering_customers = models.Offering.objects.filter(
            type=REMOTE_OFFERING, secret_options__has_keys=["api_url", "token"]
        ).values_list("customer_id", flat=True)
        return models.ServiceProvider.objects.filter(
            customer_id__in=remote_offering_customers
        ).distinct()


@shared_task(name="waldur_mastermind.marketplace_remote.pull_maintenance_announcements")
def pull_maintenance_announcements():
    """Pull and synchronize remote maintenance announcements.

    This task synchronizes maintenance announcements from remote Waldur instances,
    Runs every 60 minutes via celery beat.
    """
    MaintenanceAnnouncementListPullTask().run()
