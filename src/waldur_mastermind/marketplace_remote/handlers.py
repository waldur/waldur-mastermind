import logging

from django.conf import settings
from django.db import transaction

from waldur_auth_social.const import ProviderChoices
from waldur_core.core import middleware
from waldur_core.core.enums import ReviewStates
from waldur_core.core.utils import serialize_instance
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions import signals as permission_signals
from waldur_core.permissions.fixtures import ServiceProviderRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import REMOTE_OFFERING
from waldur_mastermind.marketplace.models import Order, Resource
from waldur_mastermind.marketplace_remote import models, tasks, utils
from waldur_mastermind.marketplace_remote.utils import INVALID_RESOURCE_STATES

logger = logging.getLogger(__name__)


def sync_permission_with_remote(sender, instance: UserRole, signal, **kwargs):
    if not settings.WALDUR_AUTH_SOCIAL["ENABLE_EDUTEAMS_SYNC"]:
        return

    if (
        instance.user.identity_source != ProviderChoices.EDUTEAMS
        and instance.user.registration_method != ProviderChoices.EDUTEAMS
    ):
        return

    # Skip synchronization of custom roles
    if not instance.role.is_system_role:
        return

    # Only project-level permissions are synced to remote Waldur instances;
    # organization owners are intentionally not propagated.
    if not isinstance(instance.scope, structure_models.Project):
        return

    # Only update remote project permissions if project has active remote resources
    if (
        not marketplace_models.Resource.objects.filter(
            project=instance.scope, offering__type=REMOTE_OFFERING
        )
        .exclude(state__in=INVALID_RESOURCE_STATES)
        .exists()
    ):
        return

    args = (
        serialize_instance(instance.scope),
        serialize_instance(instance.user),
        instance.role.name,
        signal in (permission_signals.role_granted, permission_signals.role_updated),
        instance.expiration_time and instance.expiration_time.isoformat() or None,
    )
    transaction.on_commit(
        lambda: tasks.update_remote_project_permissions.apply_async(args=args)
    )


def create_request_when_project_is_updated(
    sender, instance: Project, created=False, **kwargs
):
    if created:
        return

    # TODO: check for offering type instead
    if not settings.WALDUR_AUTH_SOCIAL["ENABLE_EDUTEAMS_SYNC"]:
        return

    user = middleware.get_current_user()

    if not user:
        return

    if not set(instance.tracker.changed()) & set(
        structure_models.PROJECT_DETAILS_FIELDS
    ):
        return

    qs = models.ProjectUpdateRequest.objects.filter(
        project=instance, state=ReviewStates.PENDING
    )
    if qs.exists():
        qs.update(state=ReviewStates.CANCELED)
    payload = {}
    for key in structure_models.PROJECT_DETAILS_FIELDS:
        payload[f"old_{key}"] = instance.tracker.previous(key)
        payload[f"new_{key}"] = getattr(instance, key)
        payload["created_by"] = user
    offering_ids = (
        marketplace_models.Resource.objects.filter(
            project=instance, offering__type=REMOTE_OFFERING
        )
        .exclude(state__in=INVALID_RESOURCE_STATES)
        .values_list("offering_id", flat=True)
        .distinct()
    )
    offerings = marketplace_models.Offering.objects.filter(id__in=offering_ids)
    for offering in offerings:
        project_request = models.ProjectUpdateRequest.objects.create(
            project=instance,
            offering=offering,
            state=ReviewStates.PENDING,
            **payload,
        )
        logger.info(
            "The project update request %s has been created by user %s",
            project_request,
            user,
        )
        # Auto-approve if possible
        if structure_permissions._has_owner_access(
            user, offering.customer
        ) or offering.customer.has_user(user, role=ServiceProviderRole.MANAGER):
            logger.info(
                "The user %s can automatically approve the request %s.",
                user,
                project_request,
            )
            project_request.approve(user, "Auto approval")
        else:
            logger.info(
                "The user %s can not automatically approve the request %s. Manual approval is required.",
                user,
                project_request,
            )


def sync_remote_project_when_request_is_approved(
    sender, instance: models.ProjectUpdateRequest, created=False, **kwargs
):
    if not settings.WALDUR_AUTH_SOCIAL["ENABLE_EDUTEAMS_SYNC"]:
        return

    if created:
        return

    if (
        not instance.tracker.has_changed("state")
        or instance.state != ReviewStates.APPROVED
    ):
        return

    transaction.on_commit(
        lambda: tasks.sync_remote_project(serialize_instance(instance))
    )


def delete_remote_project(sender, instance: Project, **kwargs):
    project = instance
    transaction.on_commit(
        lambda: tasks.delete_remote_project.delay(
            serialize_instance(project),
        )
    )


def log_request_events(
    sender, instance: models.ProjectUpdateRequest, created=False, **kwargs
):
    event_context = {"project": instance.project, "offering": instance.offering}
    if created:
        event_logger.emit(
            "Project update request has been created.",
            event_type=EventType.PROJECT_UPDATE_REQUEST_CREATED,
            event_context=event_context,
            scopes=[instance.project],
        )
        return
    if not instance.tracker.has_changed("state"):
        return
    if instance.state == ReviewStates.APPROVED:
        event_logger.emit(
            "Project update request has been approved.",
            event_type=EventType.PROJECT_UPDATE_REQUEST_APPROVED,
            event_context=event_context,
            scopes=[instance.project],
        )
    elif instance.state == ReviewStates.REJECTED:
        event_logger.emit(
            "Project update request has been rejected.",
            event_type=EventType.PROJECT_UPDATE_REQUEST_REJECTED,
            event_context=event_context,
            scopes=[instance.project],
        )


def trigger_order_callback(sender, instance: Order, created=False, **kwargs):
    """Trigger HTTP callback when marketplace order state changes."""
    if not instance.callback_url:
        return

    if not instance.tracker.has_changed("state"):
        return

    transaction.on_commit(
        lambda: tasks.trigger_order_callback.delay(serialize_instance(instance))
    )


def notify_about_project_details_update(
    sender, instance: models.ProjectUpdateRequest, created=False, **kwargs
):
    if created:
        return

    if (
        not instance.tracker.has_changed("state")
        or instance.state != ReviewStates.APPROVED
    ):
        return

    transaction.on_commit(
        lambda: tasks.notify_about_project_details_update.delay(
            serialize_instance(instance)
        )
    )


def update_remote_resource_options(sender, instance: Resource, created=False, **kwargs):
    if not instance.tracker.has_changed("options"):
        return

    if not instance.backend_id:
        return

    if instance.offering.type != REMOTE_OFFERING:
        return

    transaction.on_commit(lambda: utils.push_resource_options(instance))


def update_remote_resource_end_date(
    sender, instance: Resource, created=False, **kwargs
):
    if not instance.tracker.has_changed("end_date"):
        return

    if not instance.backend_id:
        return

    if instance.offering.type != REMOTE_OFFERING:
        return

    transaction.on_commit(lambda: utils.push_resource_end_date(instance))
