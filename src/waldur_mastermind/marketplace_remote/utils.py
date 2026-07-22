import datetime
import io
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal

import httpx
from django.core.files.base import ContentFile
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from httpx import TransportError
from rest_framework.exceptions import ValidationError
from waldur_api_client.api.marketplace_orders import (
    marketplace_orders_list,
    marketplace_orders_retrieve,
)
from waldur_api_client.api.marketplace_public_offerings import (
    marketplace_public_offerings_list,
)
from waldur_api_client.api.marketplace_resources import (
    marketplace_resources_partial_update,
    marketplace_resources_retrieve,
    marketplace_resources_team_list,
    marketplace_resources_update_options,
)
from waldur_api_client.api.marketplace_screenshots import marketplace_screenshots_list
from waldur_api_client.api.projects import (
    projects_add_user,
    projects_create,
    projects_delete_user,
    projects_list,
    projects_list_users_list,
    projects_partial_update,
    projects_update_user,
)
from waldur_api_client.api.remote_eduteams import (
    remote_eduteams as get_remote_eduteams_user,
)
from waldur_api_client.errors import UnexpectedStatus
from waldur_api_client.models.base_public_plan import BasePublicPlan
from waldur_api_client.models.offering_component import OfferingComponent
from waldur_api_client.models.order_details import (
    OrderDetails,
)
from waldur_api_client.models.order_details_field_enum import (
    OrderDetailsFieldEnum,
)
from waldur_api_client.models.patched_project_request import (
    PatchedProjectRequest,
)
from waldur_api_client.models.patched_resource_update_request import (
    PatchedResourceUpdateRequest,
)
from waldur_api_client.models.project import Project
from waldur_api_client.models.project_request import (
    ProjectRequest,
)
from waldur_api_client.models.public_offering_details import PublicOfferingDetails
from waldur_api_client.models.remote_eduteams_request_request import (
    RemoteEduteamsRequestRequest as RemoteEduteamsRequest,
)
from waldur_api_client.models.resource_options_request import ResourceOptionsRequest
from waldur_api_client.models.user_role_create_request import UserRoleCreateRequest
from waldur_api_client.models.user_role_delete_request import UserRoleDeleteRequest
from waldur_api_client.models.user_role_update_request import UserRoleUpdateRequest
from waldur_api_client.types import UNSET

from waldur_auth_social.const import ProviderChoices
from waldur_core.core.client import get_waldur_client
from waldur_core.core.utils import get_system_robot, validate_uuid
from waldur_core.media import models as media_models
from waldur_core.media import utils as media_utils
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import get_permissions
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import plugins
from waldur_mastermind.marketplace.enums import (
    REMOTE_OFFERING,
    OfferingStates,
    OrderStates,
    OrderTypes,
    RemoteResourceSyncStatus,
    ResourceStates,
)
from waldur_mastermind.marketplace_remote import models
from waldur_mastermind.marketplace_remote.constants import (
    OFFERING_COMPONENT_FIELDS,
    OFFERING_FIELDS,
    PLAN_FIELDS,
)
from waldur_mastermind.marketplace_remote.exceptions import RemoteStatusSyncFailed

logger = logging.getLogger(__name__)

INVALID_RESOURCE_STATES = (
    ResourceStates.CREATING,
    ResourceStates.TERMINATED,
)


def get_client_for_offering(offering: marketplace_models.Offering):
    client = get_waldur_client(
        offering.secret_options["api_url"],
        offering.secret_options["token"],
    )
    return client


def get_remote_user_uuid(client, username: str) -> str:
    return get_remote_eduteams_user.sync(
        client=client, body=RemoteEduteamsRequest(cuid=username)
    ).uuid.hex


def get_project_backend_id(project: structure_models.Project):
    return f"{project.customer.uuid}_{project.uuid}"


def extract_fields(fields: list[str], remote_dict: dict):
    extracted_fields = {}
    for field in fields:
        if field in remote_dict:
            extracted_fields[field] = remote_dict[field]
    return extracted_fields


def pull_fields(fields: Iterable[str], local_object, remote_dict):
    changed_fields = set()
    for field in fields:
        if field not in remote_dict:
            logger.warning(f'Remote offering does not expose field "{field}"')
            continue
        remote_value = remote_dict[field]
        local_value = getattr(local_object, field)

        if isinstance(local_value, int | float | Decimal):
            try:
                remote_value = float(remote_value)
            except (TypeError, ValueError):
                logger.warning(
                    f'Unable to convert remote value "{remote_value}" to float for field "{field}"'
                )
                continue

        if remote_value != local_value:
            setattr(local_object, field, remote_value)
            changed_fields.add(field)
    if changed_fields:
        local_object.save(update_fields=changed_fields)
    return changed_fields


def get_remote_offerings_for_project(project: structure_models.Project):
    offering_ids = (
        marketplace_models.Resource.objects.filter(
            project=project,
            offering__type=REMOTE_OFFERING,
            offering__state=OfferingStates.ACTIVE,
        )
        .exclude(state__in=INVALID_RESOURCE_STATES)
        .values_list("offering", flat=True)
        .distinct()
    )
    return marketplace_models.Offering.objects.filter(pk__in=offering_ids)


def get_projects_with_remote_offerings():
    projects_with_offerings = defaultdict(set)
    resource_pairs = (
        marketplace_models.Resource.objects.filter(offering__type=REMOTE_OFFERING)
        .exclude(state__in=INVALID_RESOURCE_STATES)
        .values("offering", "project")
        .distinct()
    )
    for pair in resource_pairs:
        try:
            project = structure_models.Project.available_objects.get(pk=pair["project"])
        except structure_models.Project.DoesNotExist:
            logger.debug(
                f"Skipping resource from a removed project with PK {pair['project']}"
            )
            continue
        offering = marketplace_models.Offering.objects.get(pk=pair["offering"])
        projects_with_offerings[project].add(offering)

    order_pairs = (
        marketplace_models.Order.objects.filter(
            offering__type=REMOTE_OFFERING,
            state__in=(
                OrderStates.PENDING_CONSUMER,
                OrderStates.PENDING_PROVIDER,
                OrderStates.EXECUTING,
            ),
        )
        .values("offering", "project")
        .distinct()
    )
    for pair in order_pairs:
        try:
            project = structure_models.Project.available_objects.get(pk=pair["project"])
        except structure_models.Project.DoesNotExist:
            logger.debug(
                f"Skipping order from a removed project with PK {pair['project']}"
            )
            continue
        offering = marketplace_models.Offering.objects.get(pk=pair["offering"])
        projects_with_offerings[project].add(offering)

    return projects_with_offerings


def get_remote_project(
    offering: marketplace_models.Offering,
    project: structure_models.Project,
    client=None,
) -> Project | None:
    if not client:
        client = get_client_for_offering(offering)
    remote_project_uuid = get_project_backend_id(project)
    remote_projects = projects_list.sync(client=client, backend_id=remote_project_uuid)
    if len(remote_projects) == 0:
        return None
    elif len(remote_projects) == 1:
        return remote_projects[0]
    else:
        raise ValidationError("There are multiple projects in remote Waldur.")


def create_remote_project(
    offering: marketplace_models.Offering,
    project: structure_models.Project,
    client=None,
):
    if not client:
        client = get_client_for_offering(offering)
    options = offering.secret_options
    remote_customer_uuid = options["customer_uuid"]
    remote_project_name = f"{project.customer.name} / {project.name}"
    remote_project_uuid = get_project_backend_id(project)
    return projects_create.sync(
        client=client,
        body=ProjectRequest(
            customer=f"{client._base_url}/api/customers/{remote_customer_uuid}/",
            name=remote_project_name,
            backend_id=remote_project_uuid,
            description=project.description,
            end_date=project.end_date,
            oecd_fos_2007_code=project.oecd_fos_2007_code,
            is_industry=project.is_industry,
            type_=project.type
            and f"{client._base_url}/api/project-types/{project.type.uuid.hex}/",
        ),
    )


def get_or_create_remote_project(
    offering: marketplace_models.Offering,
    project: structure_models.Project,
    client=None,
) -> tuple[Project, bool]:
    remote_project = get_remote_project(offering, project, client)
    if not remote_project:
        remote_project = create_remote_project(offering, project, client)
        return remote_project, True
    else:
        return remote_project, False


def update_remote_project(request: models.ProjectUpdateRequest):
    client = get_client_for_offering(request.offering)
    remote_project_name = f"{request.project.customer.name} / {request.new_name}"
    remote_project_uuid = get_project_backend_id(request.project)
    remote_projects = projects_list.sync(client=client, backend_id=remote_project_uuid)
    if len(remote_projects) == 1:
        remote_project = remote_projects[0]
        payload = dict(
            name=remote_project_name,
            description=request.new_description,
            end_date=request.new_end_date,
            oecd_fos_2007_code=request.new_oecd_fos_2007_code,
            is_industry=request.new_is_industry,
        )
        if any(getattr(remote_project, key) != value for key, value in payload.items()):
            projects_partial_update.sync(
                client=client,
                uuid=remote_project.uuid.hex,
                body=PatchedProjectRequest(**payload),
            )


def sync_resource_team(resource: marketplace_models.Resource):
    offering = resource.offering
    client = get_client_for_offering(resource.offering)
    project: structure_models.Project = resource.project
    remote_project, _ = get_or_create_remote_project(offering, project, client)

    remote_team = marketplace_resources_team_list.sync(
        client=client, uuid=resource.backend_id
    )
    remote_permissions = {
        (record.username, record.role): record.uuid.hex for record in remote_team
    }

    local_roles = UserRole.objects.filter(scope=project, is_active=True)
    local_permissions = {
        (record.user.username, record.role.name): record for record in local_roles
    }

    stale_permissions = set(remote_permissions) - set(local_permissions)

    for username, role_name in stale_permissions:
        user_uuid = remote_permissions[username, role_name]
        remove_project_permission(client, remote_project.uuid.hex, user_uuid, role_name)

    new_and_existing_users = (
        set(local_permissions) | set(remote_permissions)
    ) - stale_permissions

    for username, role_name in new_and_existing_users:
        user_uuid = remote_permissions.get((username, role_name))
        if user_uuid is None:
            user_uuid = get_remote_user_uuid(client, username)

        expiration_time = local_permissions[username, role_name].expiration_time
        create_or_update_project_permission(
            client, remote_project.uuid.hex, user_uuid, role_name, expiration_time
        )


def create_or_update_project_permission(
    client,
    remote_project_uuid: str,
    remote_user_uuid: str,
    role_name: str,
    expiration_time: datetime.datetime,
):
    permissions = projects_list_users_list.sync(
        client=client, uuid=remote_project_uuid, user=remote_user_uuid, role=role_name
    )
    if not permissions:
        return projects_add_user.sync(
            client=client,
            uuid=remote_project_uuid,
            body=UserRoleCreateRequest(
                user=remote_user_uuid, role=role_name, expiration_time=expiration_time
            ),
        )
    permission = permissions[0]
    if permission.expiration_time != expiration_time:
        return projects_update_user.sync(
            client=client,
            uuid=remote_project_uuid,
            body=UserRoleUpdateRequest(
                user=remote_user_uuid,
                role=role_name,
                expiration_time=expiration_time,
            ),
        )


def remove_project_permission(
    client, remote_project_uuid: str, remote_user_uuid: str, role_name: str
):
    remote_permissions = projects_list_users_list.sync(
        client=client, uuid=remote_project_uuid, user=remote_user_uuid, role=role_name
    )
    if remote_permissions:
        projects_delete_user.sync_detailed(
            client=client,
            uuid=remote_project_uuid,
            body=UserRoleDeleteRequest(
                user=remote_user_uuid,
                role=role_name,
            ),
        )
        return True
    return False


def sync_project_permission(
    grant,
    project: structure_models.Project,
    role_name: str,
    user,
    expiration_time: datetime.datetime,
):
    for offering in get_remote_offerings_for_project(project):
        client = get_client_for_offering(offering)
        try:
            remote_user_uuid = get_remote_user_uuid(client, user.username)

        except (UnexpectedStatus, TransportError) as e:
            logger.debug(
                f"Unable to fetch remote user {user.username} in offering {offering}: {e}"
            )
            continue

        try:
            remote_project, _ = get_or_create_remote_project(offering, project, client)
            remote_project_uuid = remote_project.uuid.hex
        except (UnexpectedStatus, TransportError) as e:
            logger.debug(
                f"Unable to create remote project {project} in offering {offering}: {e}"
            )
            continue

        if grant:
            try:
                create_or_update_project_permission(
                    client,
                    remote_project_uuid,
                    remote_user_uuid,
                    role_name,
                    expiration_time,
                )
            except (UnexpectedStatus, TransportError) as e:
                logger.debug(
                    f"Unable to create permission for user [{remote_user_uuid}] with role {role_name} (until {expiration_time}) "
                    f"and project [{remote_project_uuid}] in offering [{offering}]: {e}"
                )
        else:
            try:
                remove_project_permission(
                    client, remote_project_uuid, remote_user_uuid, role_name
                )
            except (UnexpectedStatus, TransportError) as e:
                logger.debug(
                    f"Unable to remove permission for user [{remote_user_uuid}] with role {role_name} "
                    f"and project [{remote_project_uuid}] in offering [{offering}]: {e}"
                )


def push_project_users(
    offering: marketplace_models.Offering,
    project: structure_models.Project,
    remote_project_uuid: str,
):
    client = get_client_for_offering(offering)

    permissions = collect_local_permissions(offering, project)

    for username, (role_name, expiration_time) in permissions.items():
        try:
            remote_user_uuid = get_remote_user_uuid(client, username)
        except (UnexpectedStatus, TransportError) as e:
            logger.debug(
                f"Unable to fetch remote user {username} in offering {offering}: {e}"
            )
            continue

        try:
            create_or_update_project_permission(
                client,
                remote_project_uuid,
                remote_user_uuid,
                role_name,
                expiration_time,
            )
        except (UnexpectedStatus, TransportError) as e:
            logger.debug(
                f"Unable to create permission for user [{remote_user_uuid}] with role {role_name} "
                f"and project [{remote_project_uuid}] in offering [{offering}]: {e}"
            )


def collect_local_permissions(
    offering: marketplace_models.Offering, project: structure_models.Project
) -> dict[str, tuple[str, datetime.datetime | None]]:
    permissions = defaultdict()
    for permission in get_permissions(project).filter(
        Q(role__is_system_role=True)
        & (
            Q(user__registration_method=ProviderChoices.EDUTEAMS)
            | Q(user__identity_source=ProviderChoices.EDUTEAMS)
        )
    ):
        permissions[permission.user.username] = (
            permission.role.name,
            permission.expiration_time,
        )
    # Only project-level permissions are synced to remote Waldur instances;
    # organization owners are intentionally not propagated.
    return permissions


def parse_resource_state(serialized_state: str) -> int:
    return {v: k for (k, v) in ResourceStates.CHOICES}[serialized_state]


def parse_order_state(serialized_state: str) -> int:
    return {v: k for (k, v) in OrderStates.CHOICES}[serialized_state]


def parse_order_type(serialized_state: str) -> int:
    return {v: k for (k, v) in OrderTypes.CHOICES}[serialized_state]


def parse_offering_state(serialized_state: str) -> int:
    return {
        state_name: state_id for state_id, state_name in OfferingStates.CHOICES
    }.get(serialized_state, OfferingStates.DRAFT)


def import_order(
    remote_order: OrderDetails,
    project: structure_models.Project,
    resource: marketplace_models.Resource,
    remote_order_uuid,
):
    remote_order = remote_order.to_dict()
    consumer_reviewed_at = None
    if (
        "consumer_reviewed_at" in remote_order
        and remote_order["consumer_reviewed_at"] is not None
    ):
        consumer_reviewed_at = remote_order["consumer_reviewed_at"]
    return marketplace_models.Order.objects.create(
        project=project,
        created_by=get_system_robot(),
        created=parse_datetime(remote_order["created"]),
        consumer_reviewed_by=get_system_robot(),
        consumer_reviewed_at=consumer_reviewed_at,
        resource=resource,
        type=parse_order_type(remote_order["type"]),
        offering=resource.offering,
        # NB: As a backend_id of local Order, uuid of a remote Order is used
        backend_id=remote_order_uuid,
        attributes=remote_order.get("attributes", {}),
        error_message=remote_order.get("error_message", ""),
        error_traceback=remote_order.get("error_traceback", ""),
        state=parse_order_state(remote_order["state"]),
        provider_reviewed_by=get_system_robot(),
    )


def get_new_order_ids(client, backend_id):
    remote_orders = marketplace_orders_list.sync_all(
        client=client,
        resource_uuid=backend_id,
        field=[OrderDetailsFieldEnum.UUID],
    )
    local_order_ids = set(
        marketplace_models.Order.objects.filter(
            resource__backend_id=backend_id
        ).values_list("backend_id", flat=True)
    )
    remote_order_ids = {order.uuid.hex for order in remote_orders}
    return remote_order_ids - local_order_ids


def import_resource_orders(
    resource: marketplace_models.Resource,
) -> list[marketplace_models.Order]:
    if not resource.backend_id:
        return []
    client = get_client_for_offering(resource.offering)
    new_order_ids = get_new_order_ids(client, resource.backend_id)
    imported_orders = []
    for order_id in new_order_ids:
        remote_order = marketplace_orders_retrieve.sync(client=client, uuid=order_id)
        local_order = import_order(remote_order, resource.project, resource, order_id)
        imported_orders.append(local_order)
    return imported_orders


def pull_resource_state(local_resource: marketplace_models.Resource):
    if not local_resource.backend_id:
        return
    client = get_client_for_offering(local_resource.offering)
    remote_resource = marketplace_resources_retrieve.sync(
        client=client, uuid=local_resource.backend_id
    )
    remote_state = parse_resource_state(remote_resource.state.value)
    if local_resource.state != remote_state:
        local_resource.state = remote_state
        local_resource.save(update_fields=["state"])


def import_offering_components(
    local_offering: marketplace_models.Offering,
    remote_components: list[OfferingComponent],
):
    local_components_map = {}
    for remote_component in remote_components:
        local_component, component_created = (
            marketplace_models.OfferingComponent.objects.update_or_create(
                offering=local_offering,
                type=remote_component.type_,
                defaults=extract_fields(
                    OFFERING_COMPONENT_FIELDS, remote_component.to_dict()
                ),
            )
        )
        local_components_map[local_component.type] = local_component
        logger.info(
            "Component %s (type: %s) for offering %s has been %s",
            local_component,
            local_component.type,
            local_offering,
            "created" if component_created else "updated",
        )

    return local_components_map


def import_plans(
    local_offering: marketplace_models.Offering,
    remote_plans: list[BasePublicPlan],
    local_components_map,
):
    for remote_plan in remote_plans:
        local_plan, _ = marketplace_models.Plan.objects.update_or_create(
            offering=local_offering,
            backend_id=remote_plan.uuid.hex,
            defaults=extract_fields(PLAN_FIELDS, remote_plan.to_dict()),
        )
        remote_prices = remote_plan.prices.to_dict()
        remote_quotas = remote_plan.quotas.to_dict()
        components = set(remote_prices.keys()) | set(remote_quotas.keys())
        for component_type in components:
            plan_component, component_created = (
                marketplace_models.PlanComponent.objects.update_or_create(
                    plan=local_plan,
                    component=local_components_map[component_type],
                    defaults={
                        "price": remote_prices[component_type],
                        "amount": remote_quotas[component_type],
                    },
                )
            )

            logger.info(
                "Plan component %s in offering %s has been %s",
                plan_component,
                local_offering,
                "created" if component_created else "updated",
            )


def import_offering_thumbnail(
    local_offering: marketplace_models.Offering, thumbnail_url: str | None
):
    if thumbnail_url:
        thumbnail_resp = httpx.get(thumbnail_url)
        content = ContentFile(thumbnail_resp.content)
        file_name = local_offering.uuid.hex
        if local_offering.thumbnail:
            file_object = media_models.File.objects.get(
                name=local_offering.thumbnail.name
            )
            local_file_hash = file_object.hash
            remote_file_hash = media_utils.get_image_hash(thumbnail_resp.content)
            if local_file_hash != remote_file_hash:
                local_offering.thumbnail.delete()
                local_offering.thumbnail.save(file_name, content)
        else:
            local_offering.thumbnail.save(file_name, content)
    else:
        local_offering.thumbnail.delete()
    local_offering.save(update_fields=["thumbnail"])


def push_resource_options(local_resource: marketplace_models.Resource):
    offering = local_resource.offering
    client = get_client_for_offering(offering)
    try:
        logger.info(
            f"Pushing resource {local_resource} with backend ID {local_resource.backend_id} and"
            f" options {local_resource.options} to remote Waldur"
        )
        marketplace_resources_update_options.sync(
            client=client,
            uuid=local_resource.backend_id,
            body=ResourceOptionsRequest(options=local_resource.options),
        )
    except (UnexpectedStatus, TransportError) as exc:
        logger.error("Unable to push resource options: %s", exc)


def push_resource_end_date(local_resource: marketplace_models.Resource):
    offering = local_resource.offering
    if (
        local_resource.end_date
        and local_resource.end_date < timezone.datetime.today().date()
    ):
        logger.warning(
            "Skipping push of past end date %s for resource %s",
            local_resource.end_date,
            local_resource,
        )
        return
    client = get_client_for_offering(offering)
    try:
        logger.info(
            "Pushing resource %s with backend ID %s end_date %s to remote Waldur",
            local_resource,
            local_resource.backend_id,
            local_resource.end_date,
        )
        marketplace_resources_partial_update.sync(
            client=client,
            uuid=uuid.UUID(local_resource.backend_id),
            body=PatchedResourceUpdateRequest(end_date=local_resource.end_date),
        )
    except (UnexpectedStatus, TransportError) as exc:
        logger.error("Unable to push resource end date: %s", exc)


def reconcile_resource_end_date(local_resource: marketplace_models.Resource):
    """
    Compare local and remote resource end_date and reconcile.

    If the local end_date is in the past and the remote has a valid future date,
    pull the remote date instead of pushing the stale local one.
    Otherwise, push the local value to the remote if they differ.
    """
    if (
        local_resource.offering.type != REMOTE_OFFERING
        or not local_resource.backend_id
        or local_resource.state
        in (
            ResourceStates.CREATING,
            ResourceStates.TERMINATING,
            ResourceStates.TERMINATED,
        )
    ):
        return
    client = get_client_for_offering(local_resource.offering)
    try:
        remote_resource = marketplace_resources_retrieve.sync(
            client=client, uuid=uuid.UUID(local_resource.backend_id)
        )
    except (UnexpectedStatus, TransportError) as exc:
        logger.error(
            "Unable to fetch remote resource end date reconciliation for resource %s: %s",
            local_resource,
            exc,
        )
        return

    remote_end_date = (
        None if remote_resource.end_date is UNSET else remote_resource.end_date
    )
    if remote_end_date == local_resource.end_date:
        logger.info(
            "Remote resource end date is in sync for resource %s", local_resource
        )
        return

    today = timezone.datetime.today().date()

    # If local end_date is in the past, do not push it to remote
    if local_resource.end_date and local_resource.end_date < today:
        if remote_end_date and remote_end_date >= today:
            # Pull the valid remote date instead
            old_end_date = local_resource.end_date
            local_resource.end_date = remote_end_date
            local_resource.save(update_fields=["end_date"])
            logger.info(
                "Pulled remote end date %s for resource %s (was %s)",
                remote_end_date,
                local_resource,
                old_end_date,
            )
            from waldur_mastermind.marketplace_remote import tasks as remote_tasks

            remote_events = fetch_resource_events_from_remote(local_resource)
            remote_tasks.notify_resource_end_date_pulled_from_remote.delay(
                local_resource.uuid.hex,
                str(old_end_date),
                str(remote_end_date),
                remote_events,
            )
        else:
            logger.warning(
                "Skipping push of past end date %s for resource %s "
                "(remote end_date: %s)",
                local_resource.end_date,
                local_resource,
                remote_end_date,
            )
        return

    try:
        logger.info(
            "Pushing local resource end date %s for resource %s to remote",
            local_resource.end_date,
            local_resource,
        )
        marketplace_resources_partial_update.sync(
            client=client,
            uuid=uuid.UUID(local_resource.backend_id),
            body=PatchedResourceUpdateRequest(end_date=local_resource.end_date),
        )
        return
    except (UnexpectedStatus, TransportError) as exc:
        logger.error(
            "Unable to push local resource end date %s for resource %s to remote: %s",
            local_resource.end_date,
            local_resource,
            exc,
        )
        return


def fetch_resource_events_from_remote(resource):
    """Fetch recent end_date-related events from remote Waldur for context."""
    try:
        client = get_client_for_offering(resource.offering)
        backend_uuid = str(uuid.UUID(resource.backend_id))
        scope_url = f"{client._base_url}/api/marketplace-resources/{backend_uuid}/"
        response = client.get_httpx_client().request(
            method="GET",
            url="/api/events/",
            params={
                "scope": scope_url,
                "event_type": "marketplace_resource_update_end_date_succeeded",
                "page_size": 10,
                "o": "-created",
            },
        )
        if response.status_code == 200:
            return response.json()
    except Exception as exc:
        logger.warning(
            "Unable to fetch remote events for resource %s: %s",
            resource,
            exc,
        )
    return []


def get_remote_offerings(
    client, remote_customer_uuid: str, category_uuid=None, fields=None
):
    whitelist_types = [
        offering_type
        for offering_type in plugins.manager.get_offering_types()
        if plugins.manager.enable_remote_support(offering_type)
    ]

    params = {
        "shared": True,
        "allowed_customer_uuid": remote_customer_uuid,
        "type_": whitelist_types,
    }
    if category_uuid:
        params.update({"category_uuid": category_uuid})

    if fields:
        params.update({"field": fields})
    return marketplace_public_offerings_list.sync_all(client=client, **params)


def upsert_offering(
    remote_offering: PublicOfferingDetails,
    local_category: marketplace_models.Category,
    secret_options: dict | None = None,
    local_customer: structure_models.Customer | None = None,
    local_offering: marketplace_models.Offering | None = None,
) -> marketplace_models.Offering:
    # Map the state if it exists in remote_offering
    if hasattr(remote_offering, "state") and remote_offering.state:
        state = parse_offering_state(remote_offering.state.value)
    else:
        state = OfferingStates.DRAFT  # Default state if not provided
    if local_offering:
        marketplace_models.Offering.objects.filter(id=local_offering.id).update(
            state=state,
            category=local_category,
            **extract_fields(OFFERING_FIELDS, remote_offering.to_dict()),
        )
        local_offering.refresh_from_db()
    else:
        local_offering, _ = marketplace_models.Offering.objects.update_or_create(
            type=REMOTE_OFFERING,
            backend_id=remote_offering.uuid.hex,
            customer=local_customer,
            defaults={
                "state": state,
                **extract_fields(OFFERING_FIELDS, remote_offering.to_dict()),
                "category": local_category,
                "secret_options": secret_options,
                "billable": True,
            },
        )
    # Update related data
    update_offering_related_data(local_offering, remote_offering)
    return local_offering


def import_offering_image(
    local_offering: marketplace_models.Offering, remote_offering: PublicOfferingDetails
):
    """Import offering image from remote offering"""
    image_url = remote_offering.image
    # If image URL is not provided, delete the local image
    if not image_url:
        logger.info("No image URL provided for offering %s", local_offering)
        if local_offering.image:
            local_offering.image.delete()
            local_offering.save(update_fields=["image"])
        return

    image_uuid = image_url.strip("/").split("/")[-1]

    try:
        validate_uuid(image_uuid)
    except ValidationError:
        logger.error(
            "Invalid image UUID for offering's image during sync: %s", image_uuid
        )
        return

    # Check if the image is already set by uuid
    if (
        local_offering.remote_image_uuid
        and local_offering.remote_image_uuid == uuid.UUID(image_uuid)
    ):
        return

    try:
        # Download the image from the remote offering
        image_resp = httpx.get(image_url)
        image_resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.error(
            "Failed to download image for offering %s: %s",
            local_offering,
            e,
        )
        return

    # Create a BytesIO object from the image content
    content = io.BytesIO(image_resp.content)
    # Generate a unique file name for the image
    file_name = local_offering.uuid.hex

    try:
        local_offering.remote_image_uuid = image_uuid
        local_offering.image.save(file_name, content)
        local_offering.save(update_fields=["image", "remote_image_uuid"])
    except ValueError as e:
        logger.error(
            "Failed to save image for offering %s: %s",
            local_offering,
            e,
        )


def _download_image(url: str) -> bytes:
    """Download image and return its content and hash"""
    response = httpx.get(url)
    response.raise_for_status()
    content = response.content
    return content


def import_offering_screenshots(local_offering: marketplace_models.Offering):
    """Import offering screenshots from remote offering"""
    remote_offering_uuid = local_offering.backend_id
    client = get_client_for_offering(local_offering)
    try:
        remote_screenshots = marketplace_screenshots_list.sync_all(
            client=client,
            offering_uuid=[uuid.UUID(remote_offering_uuid)],
        )
    except (UnexpectedStatus, TransportError) as e:
        logger.error(
            "Error fetching screenshots for offering %s: %s",
            remote_offering_uuid,
            e,
        )
        return

    if not remote_screenshots:
        # If no remote screenshots, delete all local ones and return
        marketplace_models.Screenshot.objects.filter(offering=local_offering).delete()
        return

    remote_screenshots_uuids = {
        remote_screenshot.uuid.hex for remote_screenshot in remote_screenshots
    }

    existing_screenshot_uuids = set(
        marketplace_models.Screenshot.objects.filter(
            offering=local_offering
        ).values_list("backend_id", flat=True)
    )
    new_screenshot_uuids = remote_screenshots_uuids - existing_screenshot_uuids
    # Process each remote screenshot
    for remote_screenshot in remote_screenshots:
        remote_screenshot_uuid = remote_screenshot.uuid.hex
        if remote_screenshot_uuid not in new_screenshot_uuids:
            continue
        try:
            # Download remote image
            response = httpx.get(remote_screenshot.image)
            response.raise_for_status()
            remote_image_content = response.content
        except httpx.HTTPError as e:
            logger.error(
                "Failed to download image for remote screenshot uuid %s, with image url %s, error: %s",
                remote_screenshot.uuid.hex,
                remote_screenshot.image,
                e,
            )
            continue
        try:
            # Create new screenshot
            content = io.BytesIO(remote_image_content)
            screenshot = marketplace_models.Screenshot.objects.create(
                offering=local_offering,
                name=remote_screenshot.name,
                description=remote_screenshot.description,
                backend_id=remote_screenshot_uuid,
            )
            screenshot.image.save(f"{screenshot.uuid.hex}", content)
        except ValueError as e:
            logger.error(
                "Failed to save image for remote screenshot uuid %s, with image url %s, error: %s",
                remote_screenshot.uuid.hex,
                remote_screenshot.image,
                e,
            )
            continue

        # Handle image thumbnail if present
        image_thumbnail_url = remote_screenshot.thumbnail
        if image_thumbnail_url:
            try:
                response = httpx.get(image_thumbnail_url)
                response.raise_for_status()
                thumbnail_content = response.content
                screenshot.thumbnail.save(
                    screenshot.uuid.hex, io.BytesIO(thumbnail_content)
                )
            except httpx.HTTPError as e:
                logger.error(
                    "Failed to download thumbnail for remote screenshot uuid %s, with thumbnail url %s, error: %s",
                    remote_screenshot.uuid.hex,
                    image_thumbnail_url,
                    e,
                )

        screenshot.save()

    # Delete local screenshots that don't exist in remote offering
    marketplace_models.Screenshot.objects.filter(offering=local_offering).exclude(
        backend_id__in=remote_screenshots_uuids
    ).delete()


def update_offering_related_data(
    local_offering: marketplace_models.Offering,
    remote_offering: PublicOfferingDetails,
):
    import_offering_image(local_offering, remote_offering)
    import_offering_screenshots(local_offering)
    import_offering_thumbnail(local_offering, remote_offering.thumbnail)

    local_components_map = import_offering_components(
        local_offering=local_offering, remote_components=remote_offering.components
    )
    import_plans(
        local_offering=local_offering,
        remote_plans=remote_offering.plans,
        local_components_map=local_components_map,
    )
    return local_offering


def get_resource_sync_status(resource):
    """
    Get resource sync status. To show the resource state in local and remote instances.
    """

    try:
        client = get_client_for_offering(resource.offering)
        remote_resource = marketplace_resources_retrieve.sync(
            client=client, uuid=resource.backend_id
        )
        if not remote_resource.state:
            return {
                "local_state": resource.get_state_display(),
                "remote_state": None,
                "sync_status": RemoteResourceSyncStatus.SYNC_FAILED,
                "error": "Remote resource state is not available",
                "last_sync": None,
            }

        status_data = {
            "local_state": resource.get_state_display(),
            "remote_state": remote_resource.state.value,
            "sync_status": RemoteResourceSyncStatus.IN_SYNC
            if resource.state == parse_resource_state(remote_resource.state.value)
            else RemoteResourceSyncStatus.OUT_OF_SYNC,
            "last_sync": timezone.now(),
        }
        return status_data
    except UnexpectedStatus as exc:
        message = f"Unable to fetch remote resource state for resource {resource.uuid}"
        logger.exception(message)
        raise RemoteStatusSyncFailed(error_message=message, error_description=str(exc))


def get_resource_team(resource: marketplace_models.Resource):
    """
    Get remote resource team. To show the resource team in local and remote instances.
    """

    try:
        client = get_client_for_offering(resource.offering)
        remote_team = marketplace_resources_team_list.sync_all(
            client=client, uuid=resource.backend_id
        )

        local_roles = UserRole.objects.filter(scope=resource.project, is_active=True)

        team_data = []
        local_roles_lookup = {
            record.user.username: record.role.name for record in local_roles
        }
        # Extract all local usernames for efficient set operations
        local_usernames = set(local_roles_lookup.keys())
        processed_local_users = set()
        for remote_record in remote_team:
            full_name = remote_record.full_name
            remote_role = remote_record.role
            username = remote_record.username
            local_role = local_roles_lookup.get(username)

            if local_role:
                sync_status = (
                    RemoteResourceSyncStatus.IN_SYNC
                    if local_role == remote_role
                    else RemoteResourceSyncStatus.OUT_OF_SYNC
                )
                processed_local_users.add(username)
            else:
                local_role = "unknown"
                sync_status = RemoteResourceSyncStatus.SYNC_FAILED

            team_data.append(
                {
                    "full_name": full_name,
                    "local_role": local_role,
                    "remote_role": remote_role,
                    "sync_status": sync_status,
                }
            )

        # Find local users that weren't processed (don't exist in remote team)
        unprocessed_local_users = local_usernames - processed_local_users

        for username in unprocessed_local_users:
            local_role_record = next(
                record for record in local_roles if record.user.username == username
            )
            team_data.append(
                {
                    "full_name": local_role_record.user.full_name,
                    "local_role": local_role_record.role.name,
                    "remote_role": "Missing from remote",
                    "sync_status": RemoteResourceSyncStatus.SYNC_FAILED,
                }
            )
        return team_data
    except UnexpectedStatus as exc:
        message = f"Unable to fetch remote team data for resource {resource.uuid}"
        logger.exception(message)
        raise RemoteStatusSyncFailed(error_message=message, error_description=str(exc))


def get_resource_order_sync_status(resource: marketplace_models.Resource):
    """
    Get remote resource order sync status. To show the resource order state in local and remote instances.
    """

    try:
        client = get_client_for_offering(resource.offering)
        remote_orders = marketplace_orders_list.sync_all(
            client=client,
            resource_uuid=resource.backend_id,
        )
        local_orders = marketplace_models.Order.objects.filter(
            resource__backend_id=resource.backend_id
        )
        local_order_ids = {
            local_order.backend_id: local_order for local_order in local_orders
        }

        order_data = []
        for remote_order in remote_orders:
            local_order = local_order_ids.get(remote_order.uuid.hex)
            if not local_order:
                order_data.append(
                    {
                        "order_uuid": remote_order.uuid.hex,
                        "remote_state": remote_order.state.value,
                        "local_state": None,
                        "sync_status": RemoteResourceSyncStatus.SYNC_FAILED,
                    }
                )
            else:
                order_data.append(
                    {
                        "order_uuid": remote_order.uuid,
                        "remote_state": remote_order.state.value,
                        "local_state": local_order.get_state_display(),
                        "sync_status": RemoteResourceSyncStatus.IN_SYNC
                        if local_order.state == parse_order_state(remote_order.state)
                        else RemoteResourceSyncStatus.OUT_OF_SYNC,
                    }
                )
        return order_data

    except UnexpectedStatus as exc:
        message = f"Unable to fetch remote order data for resource {resource.uuid}"
        logger.exception(message)
        raise RemoteStatusSyncFailed(error_message=message, error_description=str(exc))


class GenericOrderAttribute:
    def __init__(self, attrs):
        self.attrs = attrs

    def to_dict(self):
        return self.attrs


def _check_object_status(serialized_instance, model_class, field, invert=False):
    """
    Check if a serialized object exists, this method is used to check if a project or user is soft-deleted or inactive.
    """
    try:
        _, pk = serialized_instance.split(":")
        obj = model_class.all_objects.get(pk=pk)
        value = getattr(obj, field)
        if invert:
            value = not value
        return {"found": True, "flagged": value, "pk": int(pk)}
    except (ValueError, model_class.DoesNotExist):
        return {"found": False, "flagged": False, "pk": None}


def log_permission_sync_skip_reason(serialized_project, serialized_user):
    """
    Log helper function why permission sync is being skipped due to object status.

    Checks if project/user objects exist but are soft-deleted/inactive and log the reason.
    """
    project_status = _check_object_status(
        serialized_project, structure_models.Project, "is_removed"
    )
    user_status = _check_object_status(
        serialized_user, structure_models.User, "is_active", invert=True
    )
    reasons = []

    if project_status["found"]:
        if project_status["flagged"]:
            reasons.append("soft-deleted project")
    else:
        reasons.append("project not found")

    if user_status["found"]:
        if user_status["flagged"]:
            reasons.append("inactive user")
    else:
        reasons.append("user not found")

    if reasons:
        message = "Skipping permission sync: %s" % ", ".join(reasons)
    else:
        message = "Skipping permission sync: project %s or user %s not found" % (
            serialized_project,
            serialized_user,
        )
    logger.warning(
        "%s (project_id=%s, user_id=%s, project=%s, user=%s)",
        message,
        project_status["pk"],
        user_status["pk"],
        serialized_project,
        serialized_user,
    )
