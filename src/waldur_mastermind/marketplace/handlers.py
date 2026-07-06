import logging
from decimal import Decimal
from typing import Any

import httpx
from constance import config
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import signals
from django.template import Context, Template
from django.utils import timezone
from django.utils.timezone import now
from drf_spectacular.openapi import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from waldur_core.checklist import models as checklist_models
from waldur_core.core import utils as core_utils
from waldur_core.core.enums import ReviewStates
from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.models import User
from waldur_core.logging import event_logger
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import EventType, ObservableObjectType
from waldur_core.permissions import models as permission_models
from waldur_core.structure import models as structure_models
from waldur_core.structure.models import Customer, Project
from waldur_core.users import models as users_models
from waldur_core.users.enums import InvitationState
from waldur_core.users.scim import tasks as scim_tasks
from waldur_core.users.tasks import process_invitation
from waldur_freeipa.models import Profile
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    MaintenanceState,
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.enums import SCRIPT_OFFERING as SCRIPT_PLUGIN_NAME
from waldur_mastermind.marketplace.enums import (
    SITE_AGENT_OFFERING as SITE_AGENT_PLUGIN_NAME,
)
from waldur_mastermind.marketplace.log import get_order_scopes
from waldur_mastermind.marketplace.maintenance_utils import (
    MaintenanceAnnouncementTemplate,
)
from waldur_mastermind.marketplace.models import (
    Offering,
    OfferingComponent,
    OfferingUser,
    Order,
    Plan,
    PlanComponent,
    Resource,
    RobotAccount,
    ScopedServiceAccount,
    Screenshot,
)
from waldur_mastermind.marketplace.permissions import (
    order_should_not_be_reviewed_by_consumer,
)
from waldur_mastermind.notifications.models import AdminAnnouncement

from . import callbacks, log, models, order_approval, posix_ids, tasks, utils
from .tasks import remove_users_from_robot_accounts_on_permission_loss

logger = logging.getLogger(__name__)

OFFERING_USER_ALLOWED_OFFERING_TYPES = [
    BASIC_OFFERING,
    SITE_AGENT_PLUGIN_NAME,
    SCRIPT_PLUGIN_NAME,
]

ROBOT_ACCOUNT_TYPE = "Robot account"
SERVICE_ACCOUNT_TYPE = "Service account"


def create_screenshot_thumbnail(sender, instance: Screenshot, created=False, **kwargs):
    """Create a thumbnail for a screenshot."""
    if not created:
        return

    transaction.on_commit(
        lambda: tasks.create_screenshot_thumbnail.delay(instance.uuid)
    )


def get_plan_component_scopes(plan_component: models.PlanComponent):
    offering = plan_component.plan.offering
    return [offering, offering.customer]


def get_offering_component_scopes(offering_component: models.OfferingComponent):
    offering = offering_component.offering
    return [offering, offering.customer]


def get_plan_scopes(plan: models.Plan):
    offering = plan.offering
    return [offering, offering.customer]


ORDER_STATE_HANDLERS = {
    OrderStates.EXECUTING: (
        EventType.MARKETPLACE_ORDER_APPROVED,
        "Marketplace order for resource {resource_name} has been approved. Type: {type}",
    ),
    OrderStates.REJECTED: (
        EventType.MARKETPLACE_ORDER_REJECTED,
        "Marketplace order for resource {resource_name} has been rejected. Type: {type}",
    ),
    OrderStates.DONE: (
        EventType.MARKETPLACE_ORDER_COMPLETED,
        "Marketplace order for resource {resource_name} has been completed. Type: {type}",
    ),
    OrderStates.CANCELED: (
        EventType.MARKETPLACE_ORDER_TERMINATED,
        "Marketplace order for resource {resource_name} has been terminated. Type: {type}",
    ),
    OrderStates.ERRED: (
        EventType.MARKETPLACE_ORDER_FAILED,
        "Marketplace order for resource {resource_name} has been marked as failed. Type: {type}",
    ),
}

RESOURCE_TYPE_HANDLERS = {
    OrderTypes.TERMINATE: (
        EventType.MARKETPLACE_RESOURCE_TERMINATE_REQUESTED,
        "Resource {resource_name} deletion has been requested.",
    ),
    OrderTypes.UPDATE: (
        EventType.MARKETPLACE_RESOURCE_UPDATE_REQUESTED,
        "Resource {resource_name} update has been requested.",
    ),
}


def log_order_events(sender, instance: Order, created=False, **kwargs):
    """Log order creation and state transition events."""
    order = instance

    if created:
        if (
            order.state
            not in (OrderStates.PENDING_CONSUMER, OrderStates.PENDING_PROVIDER)
            or not order.resource
        ):
            return

        if order.state in RESOURCE_TYPE_HANDLERS:
            event_type, message = RESOURCE_TYPE_HANDLERS[order.state]
            event_logger.emit(
                message,
                event_type=event_type,
                event_context={"resource": order.resource},
                scopes=log.get_resource_scopes(order.resource),
            )

    elif order.tracker.has_changed("state") and order.state in ORDER_STATE_HANDLERS:
        event_type, message = ORDER_STATE_HANDLERS[order.state]
        event_logger.emit(
            message,
            event_type=event_type,
            event_context={
                "order": order,
                "type": order.get_type_display(),
                "resource_name": order.resource.name,
            },
            scopes=get_order_scopes(order),
        )


def log_resource_events(sender, instance: Resource, created=False, **kwargs):
    """Log resource creation request events."""
    resource = instance
    # Skip logging for imported resource
    if created and instance.state == ResourceStates.CREATING:
        event_logger.emit(
            "Resource {resource_name} creation has been requested.",
            event_type=EventType.MARKETPLACE_RESOURCE_CREATE_REQUESTED,
            event_context={"resource": resource},
            scopes=log.get_resource_scopes(resource),
        )


def init_resource_parent(sender, instance: Resource, created=False, **kwargs):
    """Initialize the parent resource for a newly created resource."""
    if not created or instance.tracker.has_changed("parent_id"):
        return

    resource: models.Resource = instance
    try:
        service = resource.offering.scope
    except AttributeError:
        # Skipping support offering
        return

    if isinstance(service, structure_models.BaseResource):
        base_resource = service
    elif not isinstance(service, structure_models.ServiceSettings):
        return
    else:
        base_resource = service.scope

        if not isinstance(base_resource, structure_models.BaseResource):
            return

    try:
        parent_resource = models.Resource.objects.get(scope=base_resource)
    except models.Resource.DoesNotExist:
        return

    resource.parent = parent_resource
    resource.save(update_fields=["parent"])


def notify_approvers_when_order_is_created(
    sender, instance: Order, created=False, **kwargs
):
    """Notify approvers when an order is created."""
    if get_skip_side_effects():
        return

    order: models.Order = instance
    if created and order.state in (
        OrderStates.PENDING_CONSUMER,
        OrderStates.PENDING_PROVIDER,
    ):
        if order_should_not_be_reviewed_by_consumer(order):
            order.review_by_consumer(order.created_by)
            if order.project.start_date and order.project.start_date > now().date():
                order.state = OrderStates.PENDING_PROJECT
                order.save(update_fields=["state"])
                return
            if utils.order_should_not_be_reviewed_by_provider(order):
                order.set_state_executing()
                order.save()
                logger.info(
                    "Processing order %s (%s) without approvals, resource %s",
                    order,
                    order.id,
                    order.resource,
                )
                tasks.process_order_on_commit(order, order.created_by)
            else:
                order.state = OrderStates.PENDING_PROVIDER
                order.save(update_fields=["state"])
                transaction.on_commit(
                    lambda: tasks.notify_provider_about_pending_order.delay(order.uuid)
                )
        else:
            transaction.on_commit(
                lambda: tasks.notify_consumer_about_pending_order.delay(order.uuid)
            )


def maybe_auto_approve_order_for_project(
    sender, instance: Order, created=False, **kwargs
):
    """Auto-approve a newly created PENDING_CONSUMER order if the project has
    an enabled ProjectOrderAutoApproval rule and the order qualifies.
    """
    if get_skip_side_effects():
        return
    if not created:
        return
    if instance.state != OrderStates.PENDING_CONSUMER:
        return

    order_pk = instance.pk
    transaction.on_commit(
        lambda: order_approval.try_apply_project_auto_approval(order_pk)
    )


def close_service_accounts_on_project_deletion(sender, instance: Project, **kwargs):
    """Close service accounts associated with a project when the project is deleted."""
    project: structure_models.Project = instance

    service_accounts = models.ProjectServiceAccount.objects.filter(project=project)
    if not service_accounts.exists():
        return

    for service_account in service_accounts:
        try:
            utils.close_service_account(service_account)
            service_account.delete()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                "Failed to request deletion of service account %s for project %s: %s",
                service_account,
                project,
                exc.response.text if isinstance(exc, httpx.HTTPStatusError) else exc,
            )
            continue


def close_customer_service_accounts_on_customer_deletion(
    sender, instance: Customer, **kwargs
):
    """Close service accounts associated with a customer when the customer is deleted."""
    customer: structure_models.Customer = instance
    service_accounts = models.CustomerServiceAccount.objects.filter(customer=customer)
    if not service_accounts.exists():
        return
    for service_account in service_accounts:
        try:
            utils.close_service_account(service_account)
            service_account.delete()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                "Failed to request deletion of service account %s for customer %s: %s",
                service_account,
                customer,
                exc.response.text if isinstance(exc, httpx.HTTPStatusError) else exc,
            )
            continue


def process_invitations_and_orders_when_project_start_date_is_unset(
    sender, instance: Project, created=False, **kwargs
):
    """Process pending invitations and orders when a project's start date is unset."""
    if created:
        return

    project = instance

    if not project.tracker.has_changed("start_date"):
        return

    if project.start_date:
        return

    project_content_type = ContentType.objects.get_for_model(structure_models.Project)
    invitations = users_models.Invitation.objects.filter(
        state=InvitationState.PENDING_PROJECT,
        object_id=project.id,
        content_type=project_content_type,
    )
    for invitation in invitations:
        invitation.state = InvitationState.PENDING
        invitation.save()
        sender = invitation.created_by and (
            invitation.created_by.full_name or invitation.created_by.username
        )
        transaction.on_commit(
            lambda: process_invitation.delay(invitation.uuid.hex, sender)
        )

    orders = models.Order.objects.filter(
        state=OrderStates.PENDING_PROJECT, project=project
    )
    for order in orders:
        tasks.continue_order_processing(order)


def update_resource_state_on_order_creation(
    sender, instance: Order, created=False, **kwargs
):
    """Update resource state when an order is created."""
    if not created:
        return

    if get_skip_side_effects():
        return

    order = instance
    if order.state in OrderStates.TERMINAL_STATES:
        return

    if order.resource.state == ResourceStates.ERRED:
        return

    resource = order.resource

    if order.type == OrderTypes.UPDATE and resource.state != ResourceStates.UPDATING:
        resource.set_state_updating()
        resource.save(update_fields=["state"])
    elif order.type == OrderTypes.TERMINATE:
        resource.set_state_terminating()
        resource.save(update_fields=["state"])


def update_resource_state_on_order_rejection_error_or_cancellation(
    sender, instance: Order, created=False, **kwargs
):
    """Update resource state when an order is rejected, erred or canceled."""
    order: models.Order = instance
    if not order.tracker.has_changed("state"):
        return
    resource = order.resource
    if order.state in (OrderStates.REJECTED, OrderStates.CANCELED):
        if order.type == OrderTypes.CREATE:
            resource.set_state_terminated()
            resource.save(update_fields=["state"])

        elif resource.state != ResourceStates.OK:
            resource.set_state_ok()
            resource.save(update_fields=["state"])

    elif order.state == OrderStates.ERRED:
        if resource.state != ResourceStates.CREATING:
            return
        if resource.backend_id in [None, ""]:
            logger.info("Terminating %s", resource)
            resource.set_state_terminated()
        else:
            logger.info("Setting state of %s to Erred", resource)
            resource.set_state_erred()

        resource.save(update_fields=["state"])


def sync_resource_limit_when_order(sender, instance: Order, created=False, **kwargs):
    """Synchronize resource limits when an order is created."""
    order = instance
    if order.type != OrderTypes.CREATE:
        return
    if order.resource.state != ResourceStates.CREATING:
        return
    update_fields = set()
    for prop in ("limits", "attributes", "plan_id"):
        if order.tracker.has_changed(prop):
            setattr(order.resource, prop, getattr(order, prop))
            update_fields.add(prop)
    if update_fields:
        order.resource.save(update_fields=update_fields)


def update_category_quota_when_offering_is_created(
    sender, instance: Offering, created=False, **kwargs
):
    """Update category quota when an offering is created or its state changes."""

    def get_delta():
        if created:
            if instance.state == OfferingStates.ACTIVE:
                return 1
        else:
            if instance.tracker.has_changed("state"):
                if instance.state == OfferingStates.ACTIVE:
                    return 1
                elif instance.tracker.previous("state") == OfferingStates.ACTIVE:
                    return -1

    delta = get_delta()
    if delta:
        instance.category.add_quota_usage("offering_count", delta)


def update_category_quota_when_offering_is_deleted(
    sender, instance: Offering, **kwargs
):
    """Update category quota when an offering is deleted."""
    if instance.state == OfferingStates.ACTIVE:
        instance.category.add_quota_usage("offering_count", -1)


def revoke_roles_on_offering_deletion(sender, instance: Offering, **kwargs):
    """Revoke active user roles bound to an offering before it is deleted.

    Offering-scoped roles (e.g. Offering Manager) reference the offering through
    a GenericForeignKey, which has no database-level cascade. Without this
    handler, deleting an offering leaves behind active ``UserRole`` rows whose
    scope can no longer be resolved: they appear with an empty scope in the UI
    and cannot be revoked via the scope-based ``delete_user`` endpoint.

    Running on ``pre_delete`` means the offering still exists, so the scope
    resolves and the revocation is logged normally.
    """
    for permission in permission_models.UserRole.objects.filter(
        scope=instance, is_active=True
    ):
        permission.revoke(reason="Offering deletion")


def update_category_offerings_count(sender, **kwargs):
    """Update the count of offerings for each category."""
    for category in models.Category.objects.all():
        value = models.Offering.objects.filter(
            category=category, state=OfferingStates.ACTIVE
        ).count()
        category.set_quota_usage("offering_count", value)


def delete_service_setting_when_offering_is_deleted(
    sender, instance: Offering, **kwargs
):
    """Delete service settings when an offering is deleted."""
    offering: models.Offering = instance
    try:
        service_settings = offering.scope
    except AttributeError:
        return

    if not isinstance(service_settings, structure_models.ServiceSettings):
        return

    service_settings.delete()


def create_resource_plan_period_when_resource_is_created(
    sender, instance: Resource, created=False, **kwargs
):
    """Create a resource plan period when a resource is created."""
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state != ResourceStates.OK:
        return

    if instance.tracker.previous("state") != ResourceStates.CREATING:
        return

    if not instance.plan:
        return

    callbacks.create_resource_plan_period(instance)


def close_resource_plan_period_when_resource_is_terminated(
    sender, instance: Resource, created=False, **kwargs
):
    """
    Handle case when resource has been terminated by service provider.
    """

    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state != ResourceStates.TERMINATED:
        return

    if not instance.plan:
        return

    callbacks.close_resource_plan_period(instance)


def soft_delete_resource_projects_when_resource_is_terminated(
    sender, instance: Resource, created=False, **kwargs
):
    """Cascade Resource → TERMINATED into a soft-delete of its child ResourceProjects.

    System-driven, so removed_by stays None to distinguish from user-initiated
    deletions in the audit trail.
    """
    if created:
        return
    if not instance.tracker.has_changed("state"):
        return
    if instance.state != ResourceStates.TERMINATED:
        return
    for resource_project in instance.projects.filter(is_removed=False):
        resource_project.delete(soft=True, terminated_by=None)


def switch_resource_plan_period_when_plan_is_updated(
    sender, instance: Resource, created=False, **kwargs
):
    """Switch the resource plan period when a resource's plan is updated."""
    if created:
        return

    if not instance.tracker.has_changed("plan_id"):
        return

    previous_plan_id = instance.tracker.previous("plan_id")
    if previous_plan_id:
        models.ResourcePlanPeriod.objects.filter(
            resource=instance,
            plan_id=previous_plan_id,
            end=None,
        ).update(end=now())

    if instance.plan:
        callbacks.create_resource_plan_period(instance)


def change_order_state(sender, instance, created=False, **kwargs):
    """Change the state of an order based on resource state changes."""
    if created or not instance.tracker.has_changed("state"):
        return

    try:
        resource = models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.warning(
            "Skipping resource state synchronization "
            "because marketplace resource is not found. "
            "Resource ID: %s",
            core_utils.serialize_instance(instance),
        )
    else:
        callbacks.sync_resource_state(instance, resource)


def terminate_resource(sender, instance, **kwargs):
    """Terminate a resource."""
    try:
        resource = models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping terminate for resource "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            core_utils.serialize_instance(instance),
        )
    else:
        callbacks.resource_deletion_succeeded(resource)


def connect_resource_handlers(*resources):
    for index, model in enumerate(resources):
        suffix = f"{index}_{model.__class__}"

        signals.post_save.connect(
            change_order_state,
            sender=model,
            dispatch_uid="waldur_mastermind.marketplace.change_order_state_%s" % suffix,
        )

        signals.pre_delete.connect(
            terminate_resource,
            sender=model,
            dispatch_uid="waldur_mastermind.marketplace.terminate_resource_%s" % suffix,
        )


def synchronize_resource_metadata_on_save(sender, instance, created=False, **kwargs):
    """Synchronize resource metadata on save."""
    fields = {
        "action",
        "action_details",
        "state",
        "runtime_state",
        "name",
        "backend_id",
    }
    if not created and not set(instance.tracker.changed()) & fields:
        return

    try:
        resource = models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping resource synchronization for OpenStack resource "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            instance.id,
        )
        return

    utils.import_resource_metadata(resource)


def synchronize_resource_metadata_on_delete(sender, instance, **kwargs):
    """Synchronize resource metadata on delete."""
    try:
        resource = models.Resource.objects.get(scope=instance)
    except ObjectDoesNotExist:
        logger.debug(
            "Skipping resource synchronization for OpenStack resource "
            "because marketplace resource does not exist. "
            "Resource ID: %s",
            instance.id,
        )
        return
    resource.backend_metadata = {"state": "Deleted"}
    resource.save()


def connect_resource_metadata_handlers(*resources):
    for index, model in enumerate(resources):
        signals.post_save.connect(
            synchronize_resource_metadata_on_save,
            sender=model,
            dispatch_uid="waldur_mastermind.marketplace."
            f"synchronize_resource_metadata_on_save_{index}_{model.__class__}",
        )

        signals.post_delete.connect(
            synchronize_resource_metadata_on_delete,
            sender=model,
            dispatch_uid="waldur_mastermind.marketplace."
            f"synchronize_resource_metadata_on_delete_{index}_{model.__class__}",
        )


def sync_current_usages_from_component_usage(
    sender, instance: models.ComponentUsage, **kwargs
):
    """Update resource.current_usages for the saved component.

    Triggered on every ComponentUsage save. Uses the latest record
    (by billing_period) for the component that was just saved, and
    updates only that entry in current_usages.

    Replaces the pattern where each backend handler manually set
    current_usages — that approach caused stale values because the
    snapshot was never reset across period boundaries.
    """
    resource = instance.resource
    component_type = instance.component.type

    latest = (
        models.ComponentUsage.objects.filter(
            resource=resource, component=instance.component
        )
        .order_by("-billing_period")
        .first()
    )
    if not latest:
        return

    new_value = float(latest.usage)
    current = resource.current_usages or {}
    if current.get(component_type) != new_value:
        current[component_type] = new_value
        resource.current_usages = current
        resource.save(update_fields=["current_usages"])


def update_or_create_quotas(resource: Resource):
    components_map = resource.offering.get_limit_components()
    for key, value in resource.limits.items():
        component = components_map.get(key)
        if component:
            models.ComponentQuota.objects.update_or_create(
                resource=resource, component=component, defaults={"limit": value}
            )


def sync_limits(sender, instance: Resource, created=False, **kwargs):
    """Synchronize resource limits."""
    if not created and not instance.tracker.has_changed("limits"):
        return
    transaction.on_commit(lambda: update_or_create_quotas(instance))


@transaction.atomic()
def limit_update_succeeded(sender, order: models.Order, **kwargs):
    """Handle successful limit updates."""
    resource = order.resource
    old_limits = resource.limits
    resource.limits = order.limits
    if resource.state != ResourceStates.OK:
        resource.set_state_ok()
    resource.save()
    order.complete()
    order.save(update_fields=["state"])
    logger.info(
        "Resource limits have been updated. Resource: %s, old limits: %s, new limits: %s, created by: %s",
        core_utils.serialize_instance(resource),
        old_limits,
        resource.limits,
        order.created_by,
    )
    log.log_resource_limit_update_succeeded(resource)

    if resource.downscaled or resource.paused:
        resource_uuid = str(resource.uuid)
        offering_id = resource.offering_id
        transaction.on_commit(
            lambda: _trigger_slurm_policy_reevaluation(resource_uuid, offering_id)
        )


def _trigger_slurm_policy_reevaluation(resource_uuid, offering_id):
    """Trigger immediate SLURM policy re-evaluation after limit changes.

    When resource limits increase on a downscaled/paused resource,
    the policy system needs to re-evaluate usage percentages so that
    QoS restrictions are lifted promptly instead of waiting for the
    next periodic evaluation cycle.
    """
    from waldur_mastermind.policy import models as policy_models
    from waldur_mastermind.policy import tasks as policy_tasks

    policies = policy_models.SlurmPeriodicUsagePolicy.objects.filter(
        scope_id=offering_id,
    )
    for policy in policies:
        policy_tasks.evaluate_resource_against_policy.delay(
            resource_uuid, str(policy.uuid)
        )


def limit_update_failed(sender, order: models.Order, error_message, **kwargs):
    """Handle failed limit updates."""
    order.set_state_erred()
    order.error_message = error_message
    order.save()
    resource = order.resource
    logger.info(
        "Resource limit update failed. Resource: %s, requested limits: %s, created by: %s, "
        "error message: %s",
        core_utils.serialize_instance(resource),
        resource.limits,
        order.created_by,
        error_message,
    )
    event_logger.emit(
        "Updating limits of resource {resource_name} has failed.",
        event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_LIMITS_FAILED,
        event_context={"resource": resource},
        scopes=log.get_resource_scopes(resource),
    )


def update_customer_of_offering_if_project_has_been_moved(
    sender, project, old_customer, new_customer, **kwargs
):
    """Update the customer of an offering if the project has been moved."""
    for offering in models.Offering.objects.filter(
        project=project, customer=old_customer
    ):
        offering.customer = new_customer
        offering.save()

        #  Make sure that scope has an actual field customer, not property
        if (
            offering.scope
            and hasattr(offering.scope.__class__, "customer")
            and hasattr(offering.scope.__class__.customer, "field")
        ):
            offering.scope.customer = new_customer
            offering.scope.save()


def disable_empty_service_settings(offering):
    service_settings = getattr(offering, "scope", None)
    if not service_settings:
        return

    if not isinstance(service_settings, structure_models.ServiceSettings):
        return

    if (
        not models.Resource.objects.filter(offering=offering)
        .exclude(state=ResourceStates.TERMINATED)
        .exists()
    ):
        service_settings.is_active = False
        service_settings.save(update_fields=["is_active"])


def enable_nonempty_service_settings(offering):
    service_settings = getattr(offering, "scope", None)
    if not service_settings:
        return

    if not isinstance(service_settings, structure_models.ServiceSettings):
        return

    if (
        models.Resource.objects.filter(offering=offering)
        .exclude(state=ResourceStates.TERMINATED)
        .exists()
    ):
        service_settings.is_active = True
        service_settings.save(update_fields=["is_active"])


def disable_archived_service_settings_without_existing_resource(
    sender, instance: Resource, created=False, **kwargs
):
    """Disable archived service settings if there are no existing resources."""
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state != ResourceStates.TERMINATED:
        return

    offering: models.Offering = instance.offering

    if offering.state != OfferingStates.ARCHIVED:
        return

    disable_empty_service_settings(offering)


def disable_service_settings_without_existing_resource_when_archived(
    sender, instance: Offering, created=False, **kwargs
):
    """Disable service settings without existing resources when an offering is archived."""
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state != OfferingStates.ARCHIVED:
        return

    disable_empty_service_settings(instance)


def enable_service_settings_with_existing_resource(
    sender, instance: Resource, created=False, **kwargs
):
    """Enable service settings if there are existing resources."""
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state in [
        ResourceStates.TERMINATED,
        ResourceStates.TERMINATING,
    ]:
        return

    enable_nonempty_service_settings(instance.offering)


def enable_service_settings_when_not_archived(
    sender, instance: Offering, created=False, **kwargs
):
    """Enable service settings when an offering is not archived."""
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state == OfferingStates.ARCHIVED:
        return

    enable_nonempty_service_settings(instance)


def plan_component_has_been_updated(
    sender, instance: PlanComponent, created=False, **kwargs
):
    """Log plan component updates."""
    if created:
        return

    if instance.tracker.has_changed("price"):
        event_logger.emit(
            f"Current price of component {instance.component.type} in plan {instance.plan.name} has been updated.",
            event_type=EventType.MARKETPLACE_PLAN_COMPONENT_CURRENT_PRICE_UPDATED,
            event_context={
                "plan_component": instance,
                "old_value": instance.tracker.previous("price"),
                "new_value": Decimal(instance.price)
                if isinstance(instance.price, str)
                else instance.price,
            },
            scopes=get_plan_component_scopes(instance),
        )
    if instance.tracker.has_changed("future_price"):
        event_logger.emit(
            f"Future price of component {instance.component.type} in plan {instance.plan.name} has been updated.",
            event_type=EventType.MARKETPLACE_PLAN_COMPONENT_FUTURE_PRICE_UPDATED,
            event_context={
                "plan_component": instance,
                "old_value": instance.tracker.previous("future_price"),
                "new_value": Decimal(instance.future_price)
                if isinstance(instance.future_price, str)
                else instance.future_price,
            },
            scopes=get_plan_component_scopes(instance),
        )
    if instance.tracker.has_changed("amount"):
        event_logger.emit(
            f"Quota of component {instance.component.type} in plan {instance.plan.name} has been updated.",
            event_type=EventType.MARKETPLACE_PLAN_COMPONENT_QUOTA_UPDATED,
            event_context={
                "plan_component": instance,
                "old_value": instance.tracker.previous("amount"),
                "new_value": Decimal(instance.amount)
                if isinstance(instance.amount, str)
                else instance.amount,
            },
            scopes=get_plan_component_scopes(instance),
        )


def offering_component_has_been_created_or_updated(
    sender, instance: OfferingComponent, created=False, **kwargs
):
    """Log offering component creation and updates."""
    if created:
        event_logger.emit(
            f"Offering component {instance.name} has been created.",
            event_type=EventType.MARKETPLACE_OFFERING_COMPONENT_CREATED,
            event_context={
                "offering_component": instance,
            },
            scopes=get_offering_component_scopes(instance),
        )
    else:
        changes = [
            f"{field}: {instance.tracker.previous(field)} -> {getattr(instance, field, None)}"
            for field in instance.tracker.changed()
        ]
        if changes:
            diff = ", ".join(changes)
            event_logger.emit(
                f"Offering component {instance.name} has been updated. Details: {diff}.",
                event_type=EventType.MARKETPLACE_OFFERING_COMPONENT_UPDATED,
                event_context={
                    "offering_component": instance,
                },
                scopes=get_offering_component_scopes(instance),
            )


def offering_component_has_been_deleted(sender, instance: OfferingComponent, **kwargs):
    """Log offering component deletion."""
    event_logger.emit(
        f"Offering component {instance.name} has been deleted.",
        event_type=EventType.MARKETPLACE_OFFERING_COMPONENT_DELETED,
        event_context={
            "offering_component": instance,
        },
        scopes=get_offering_component_scopes(instance),
    )


def plan_has_been_created_or_updated(sender, instance: Plan, created=False, **kwargs):
    """Log plan creation, update, and archiving events."""
    if created:
        event_logger.emit(
            f"Plan {instance.name} has been created.",
            event_type=EventType.MARKETPLACE_PLAN_CREATED,
            event_context={
                "plan": instance,
            },
            scopes=get_plan_scopes(instance),
        )
    else:
        if instance.tracker.has_changed("archived"):
            event_logger.emit(
                f"Plan {instance.name} has been archived.",
                event_type=EventType.MARKETPLACE_PLAN_ARCHIVED,
                event_context={
                    "plan": instance,
                },
                scopes=get_plan_scopes(instance),
            )
        else:
            excluded_fields = {"modified", "created"}
            changes = [
                f"{field}: {instance.tracker.previous(field)} -> {getattr(instance, field, None)}"
                for field in instance.tracker.changed()
                if field not in excluded_fields
            ]
            if changes:
                diff = ", ".join(changes)
                event_logger.emit(
                    f"Plan {instance.name} has been updated. Details: {diff}.",
                    event_type=EventType.MARKETPLACE_PLAN_UPDATED,
                    event_context={
                        "plan": instance,
                    },
                    scopes=get_plan_scopes(instance),
                )


def _summarize_offering_form_options_diff(old_value, new_value) -> str:
    old_options = (old_value or {}).get("options") or {}
    new_options = (new_value or {}).get("options") or {}
    old_order = (old_value or {}).get("order") or []
    new_order = (new_value or {}).get("order") or []

    parts = []
    added = sorted(set(new_options) - set(old_options))
    removed = sorted(set(old_options) - set(new_options))
    changed = sorted(
        key
        for key in set(old_options) & set(new_options)
        if old_options[key] != new_options[key]
    )
    if added:
        parts.append(f"added option keys: {', '.join(added)}")
    if removed:
        parts.append(f"removed option keys: {', '.join(removed)}")
    if changed:
        parts.append(f"changed option keys: {', '.join(changed)}")
    if old_order != new_order:
        parts.append("option order changed")
    return ". ".join(parts) + "." if parts else "No changes detected."


def offering_has_been_created_or_updated(
    sender, instance: Offering, created=False, **kwargs
):
    """Log offering creation and updates."""
    if created:
        event_logger.emit(
            "Offering has been created.",
            event_type=EventType.MARKETPLACE_OFFERING_CREATED,
            event_context={
                "offering": instance,
            },
            scopes=[instance, instance.customer],
        )
    else:
        if instance.tracker.has_changed("state"):
            event_logger.emit(
                "Offering state has been updated.",
                event_type=EventType.MARKETPLACE_OFFERING_UPDATED,
                event_context={
                    "offering": instance,
                    "old_value": models.Offering(
                        state=instance.tracker.previous("state")
                    ).get_state_display(),
                    "new_value": instance.get_state_display(),
                },
                scopes=[instance, instance.customer],
            )

        if instance.tracker.has_changed("options"):
            changes_summary = _summarize_offering_form_options_diff(
                instance.tracker.previous("options"), instance.options
            )
            event_logger.emit(
                f"Offering {instance.name} order form options have been updated. "
                f"Details: {changes_summary}",
                event_type=EventType.MARKETPLACE_OFFERING_OPTIONS_UPDATED,
                event_context={
                    "offering": instance,
                    "changes_summary": changes_summary,
                },
                scopes=[instance, instance.customer],
            )

        if instance.tracker.has_changed("resource_options"):
            changes_summary = _summarize_offering_form_options_diff(
                instance.tracker.previous("resource_options"),
                instance.resource_options,
            )
            event_logger.emit(
                f"Offering {instance.name} resource report form options have been "
                f"updated. Details: {changes_summary}",
                event_type=EventType.MARKETPLACE_OFFERING_RESOURCE_OPTIONS_UPDATED,
                event_context={
                    "offering": instance,
                    "changes_summary": changes_summary,
                },
                scopes=[instance, instance.customer],
            )


def resource_has_been_changed(sender, instance: Resource, created=False, **kwargs):
    """Log resource changes."""
    if created:
        return

    changed_fields = instance.tracker.changed().copy()
    for field in models.Resource.NON_LOGGABLE_FIELDS:
        changed_fields.pop(field, None)

    if not changed_fields:
        return

    changed = []

    def get_relative_object_name(field_name, value):
        if not value:
            return ""

        try:
            return str(
                getattr(models.Resource, field_name.replace("_id", ""))
                .get_queryset()
                .get(id=value)
            )
        except ObjectDoesNotExist:
            return ""

    for field, old_value in sorted(changed_fields.items()):
        if field in ["last_sync"]:
            continue

        if field == "state":
            old_value_display = models.Resource(state=old_value).get_state_display()
            new_value_display = instance.get_state_display()
            # Skip if display values are equal
            if old_value_display == new_value_display:
                continue
            old_value = old_value_display
            new_value = new_value_display
        elif field == "project_id":
            old_value_display = get_relative_object_name("project_id", old_value)
            new_value_display = get_relative_object_name(
                "project_id", getattr(instance, field)
            )
            if old_value_display == new_value_display:
                continue
            old_value = old_value_display
            new_value = new_value_display
        elif field == "offering_id":
            old_value_display = get_relative_object_name("offering_id", old_value)
            new_value_display = get_relative_object_name(
                "offering_id", getattr(instance, field)
            )
            if old_value_display == new_value_display:
                continue
            old_value = old_value_display
            new_value = new_value_display
        else:
            new_value = getattr(instance, field)
            # More robust comparison to handle date fields and other types
            if str(old_value) == str(new_value):
                continue

        if not old_value and not new_value:
            continue
        changed.append({"name": field, "from": old_value, "to": new_value})

    if not changed:
        return

    resource = instance

    template = (
        "Marketplace resource '{{ resource_name }}' has been changed. "
        "{% for field in changed %}"
        "{% if forloop.first %}Field{% else %}field{% endif %} "
        "'{{ field.name }}': from {{ field.from|safe }} to {{ field.to|safe }}"
        "{% if forloop.last %}.{% else %}, {% endif %}"
        "{% endfor %}"
    )

    context = {
        "resource_name": resource.name,
        "changed": changed,
    }

    event_logger.emit(
        Template(template)
        .render(Context(context))
        .replace("{", "{{")
        .replace("}", "}}"),
        event_type=EventType.MARKETPLACE_RESOURCE_UPDATE_SUCCEEDED,
        event_context={
            "resource": resource,
        },
        scopes=log.get_resource_scopes(resource),
    )

    if not config.DISABLE_SENDING_NOTIFICATIONS_ABOUT_RESOURCE_UPDATE:
        transaction.on_commit(
            lambda: tasks.notify_about_resource_change.delay(
                EventType.MARKETPLACE_RESOURCE_UPDATE_SUCCEEDED, context, resource.uuid
            )
        )


def resource_state_has_been_changed(
    sender, instance: Resource, created=False, **kwargs
):
    """Handle resource state changes."""
    if created:
        return

    resource = instance

    if not resource.tracker.has_changed("state"):
        return

    if (
        resource.state == ResourceStates.OK
        and resource.tracker.previous("state") == ResourceStates.ERRED
        and resource.error_traceback
    ):
        resource.error_traceback = ""
        resource.save()


def delete_expired_project_if_every_resource_has_been_terminated(
    sender, instance: Resource, created=False, **kwargs
):
    """Delete an expired project if all its resources have been terminated."""
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state != ResourceStates.TERMINATED:
        return

    # Ensure customer relationship is loaded to avoid KeyError during quota cleanup
    project = instance.project
    try:
        # Test if customer relationship is accessible
        _ = project.customer
    except (AttributeError, KeyError):
        # Reload project with customer relationship if not accessible
        project = project.__class__.objects.select_related("customer").get(
            pk=project.pk
        )

    if project.is_expired:
        resources = (
            models.Resource.objects.filter(project=project)
            .exclude(
                state__in=(
                    ResourceStates.ERRED,
                    ResourceStates.TERMINATED,
                )
            )
            .exists()
        )
        if not resources:
            event_logger.emit(
                "Project {project_name} is going to be deleted because end date has been reached and there are no active resources.",
                event_type=EventType.PROJECT_DELETION_TRIGGERED,
                event_context={"project": project},
                scopes=[project, project.customer],
            )
            project.delete()


def log_offering_user_created(sender, instance: OfferingUser, created=False, **kwargs):
    """Log offering user creation."""
    if not created:
        return
    event_logger.emit(
        f"Account for user {instance.user.username} in offering {instance.offering.name} has been created.",
        event_type=EventType.MARKETPLACE_OFFERING_USER_CREATED,
        event_context={"offering_user": instance},
        scopes=[instance.offering, instance.offering.customer],
    )


def log_offering_user_deleted(sender, instance: OfferingUser, **kwargs):
    """Log offering user deletion."""
    event_logger.emit(
        f"Account for user {instance.user.username} in offering {instance.offering.name} has been deleted.",
        event_type=EventType.MARKETPLACE_OFFERING_USER_DELETED,
        event_context={"offering_user": instance},
        scopes=[instance.offering, instance.offering.customer],
    )


def log_offering_user_username_updated(
    sender, instance: OfferingUser, created=False, **kwargs
):
    if created:
        return
    if not instance.tracker.has_changed("username"):
        return

    old_username = instance.tracker.previous("username")
    new_username = instance.username

    event_logger.emit(
        "Offering user username changed for {offering_user_uuid}: '{old_username}' -> '{new_username}'.",
        event_type=EventType.MARKETPLACE_OFFERING_USER_UPDATED,
        event_context={
            "offering_user_uuid": instance.uuid.hex,
            "old_username": old_username or "",
            "new_username": new_username or "",
            "changed_fields": ["username"],
            "offering": instance.offering,
            "affected_user": instance.user,
        },
        scopes=[instance.offering, instance.offering.customer],
    )


def create_offering_user_checklist_completions(
    sender, instance: OfferingUser, created=False, **kwargs
):
    """Create checklist completions for OfferingUser when created."""
    if not created:
        return

    offering_user = instance
    offering = offering_user.offering

    # Get compliance checklist associated with the offering
    checklist = offering.compliance_checklist

    if not checklist:
        logger.debug(
            "No compliance checklist configured for offering %s",
            offering.name,
        )
        return

    # Get content type for OfferingUser
    offering_user_content_type = ContentType.objects.get_for_model(OfferingUser)

    # Create a ChecklistCompletion for the checklist
    try:
        checklist_models.ChecklistCompletion.objects.create(
            scope_content_type=offering_user_content_type,
            scope_object_id=offering_user.id,
            checklist=checklist,
        )
        logger.info(
            "Created checklist completion for %s, checklist: %s",
            offering_user,
            checklist.name,
        )
    except Exception as e:
        logger.error(
            "Failed to create checklist completion for %s, checklist: %s. Error: %s",
            offering_user,
            checklist.name,
            e,
        )


def create_checklist_completions_for_existing_users(
    sender, instance: Offering, created=False, **kwargs
):
    """Manage checklist completions for existing OfferingUsers when compliance changes."""
    if created:
        return  # New offerings are handled by the OfferingUser creation handler

    # Check if compliance_checklist changed
    if not instance.tracker.has_changed("compliance_checklist"):
        return

    old_checklist = instance.tracker.previous("compliance_checklist")
    new_checklist = instance.compliance_checklist

    from waldur_mastermind.marketplace import tasks

    # Handle different scenarios
    if old_checklist is None and new_checklist is not None:
        # Adding compliance checklist (None → checklist)
        tasks.create_checklist_completions_for_offering_users.delay(
            offering_id=instance.id, checklist_id=new_checklist.id
        )
        logger.info(
            "Queued background task to create checklist completions for offering %s with checklist %s",
            instance.name,
            new_checklist.name,
        )

    elif old_checklist is not None and new_checklist is None:
        # Removing compliance checklist (checklist → None)
        # old_checklist might be an ID, so get the object if needed
        old_checklist_id = (
            old_checklist if isinstance(old_checklist, int) else old_checklist.id
        )
        old_checklist_name = (
            old_checklist.name
            if hasattr(old_checklist, "name")
            else f"Checklist ID {old_checklist_id}"
        )

        tasks.remove_checklist_completions_for_offering_users.delay(
            offering_id=instance.id, checklist_id=old_checklist_id
        )
        logger.info(
            "Queued background task to remove checklist completions for offering %s with old checklist %s",
            instance.name,
            old_checklist_name,
        )

    elif old_checklist is not None and new_checklist is not None:
        # Handle both ID and object cases for comparison
        old_checklist_id = (
            old_checklist if isinstance(old_checklist, int) else old_checklist.id
        )
        new_checklist_id = new_checklist.id

        if old_checklist_id != new_checklist_id:
            # Changing checklist (checklist_A → checklist_B)
            old_checklist_name = (
                old_checklist.name
                if hasattr(old_checklist, "name")
                else f"Checklist ID {old_checklist_id}"
            )

            tasks.replace_checklist_completions_for_offering_users.delay(
                offering_id=instance.id,
                old_checklist_id=old_checklist_id,
                new_checklist_id=new_checklist_id,
            )
            logger.info(
                "Queued background task to replace checklist completions for offering %s: %s → %s",
                instance.name,
                old_checklist_name,
                new_checklist.name,
            )


def delete_offering_user_checklist_completions(
    sender, instance: OfferingUser, **kwargs
):
    """Delete related checklist completions when OfferingUser is deleted."""
    offering_user = instance
    offering_user_content_type = ContentType.objects.get_for_model(OfferingUser)

    # Delete all related checklist completions
    deleted_count = checklist_models.ChecklistCompletion.objects.filter(
        scope_content_type=offering_user_content_type,
        scope_object_id=offering_user.id,
    ).delete()[0]

    if deleted_count > 0:
        logger.info(
            "Deleted %d checklist completion(s) for %s",
            deleted_count,
            offering_user,
        )


def generate_changes_string(changed_dict, instance, account_type):
    changes_string = ""
    if "username" in changed_dict:
        changes_string += (
            f"{account_type} {changed_dict['username']} has been updated. "
        )
    else:
        changes_string += f"{account_type} {instance.username} has been updated. "
    for key in changed_dict:
        if key == "state":
            if account_type == ROBOT_ACCOUNT_TYPE:
                old_state = models.RobotAccount(
                    state=changed_dict[key]
                ).get_state_display()
            new_state = instance.get_state_display()
            change_string = f"{account_type} '{instance.username}' state changed from '{old_state}' to '{new_state}'."
        else:
            change_string = f"{key} had changed from {changed_dict[key]} to {getattr(instance, key)}. "
        changes_string += change_string
    return changes_string


def log_service_account_created_or_updated(
    sender, instance: ScopedServiceAccount, created=False, **kwargs
):
    """Log service account creation and updates."""
    if not created:
        changed_string = generate_changes_string(
            instance.tracker.changed(), instance, SERVICE_ACCOUNT_TYPE
        )
        event_logger.emit(
            changed_string,
            event_type=EventType.SERVICE_ACCOUNT_UPDATED,
            event_context={"service_account": instance},
            scopes=[instance, instance.scope],
        )
        return
    event_logger.emit(
        "Service account {service_account_username} has been created.",
        event_type=EventType.SERVICE_ACCOUNT_CREATED,
        event_context={"service_account": instance},
        scopes=[instance, instance.scope],
    )


def log_service_account_deleted(sender, instance: ScopedServiceAccount, **kwargs):
    """Log service account deletion."""
    event_logger.emit(
        "Service account {service_account_username} has been deleted.",
        event_type=EventType.SERVICE_ACCOUNT_DELETED,
        event_context={"service_account": instance},
        scopes=[instance, instance.scope],
    )


def log_resource_robot_account_created_or_updated(
    sender, instance: RobotAccount, created=False, **kwargs
):
    """Log resource robot account creation and updates."""
    if not created:
        changed_string = generate_changes_string(
            instance.tracker.changed(), instance, ROBOT_ACCOUNT_TYPE
        )
        event_logger.emit(
            changed_string,
            event_type=EventType.RESOURCE_ROBOT_ACCOUNT_UPDATED,
            event_context={"robot_account": instance},
            scopes=[instance, instance.resource],
        )
        return
    event_logger.emit(
        "Robot account {robot_account_username} has been created.",
        event_type=EventType.RESOURCE_ROBOT_ACCOUNT_CREATED,
        event_context={"robot_account": instance},
        scopes=[instance, instance.resource],
    )


def log_resource_robot_account_deleted(sender, instance: RobotAccount, **kwargs):
    """Log resource robot account deletion."""
    event_logger.emit(
        "Robot account {robot_account_username} has been deleted.",
        event_type=EventType.RESOURCE_ROBOT_ACCOUNT_DELETED,
        event_context={"robot_account": instance},
        scopes=[instance, instance.resource],
    )


def create_offering_users_when_project_role_granted(sender, instance, **kwargs):
    """Schedule task to create or restore offering users when project role is granted."""
    if not isinstance(instance.scope, structure_models.Project):
        return
    project = instance.scope
    user = instance.user
    # Schedule task after transaction commits to avoid heavy queries in transaction
    transaction.on_commit(
        lambda: tasks.create_or_restore_offering_users_for_user.delay(
            user.uuid.hex, project.uuid.hex
        )
    )


def request_offering_user_deletion_when_project_access_lost(sender, instance, **kwargs):
    """Schedule task to request offering user deletion when project access is lost."""
    if not isinstance(instance.scope, structure_models.Project):
        return
    user = instance.user
    transaction.on_commit(
        lambda: tasks.request_offering_user_deletion_for_user.delay(user.uuid.hex)
    )


def create_offering_user_for_new_resource(sender, instance: Resource, **kwargs):
    """Defer offering user creation to Celery after resource creation succeeds."""
    resource = instance
    project = resource.project
    offering = resource.offering
    if offering.type not in OFFERING_USER_ALLOWED_OFFERING_TYPES:
        logger.info(
            "The offering %s does not support offering users feature.", offering
        )
        return

    if not offering.plugin_options.get("service_provider_can_create_offering_user"):
        logger.info(
            "It is not allowed to create users for current offering %s.", offering
        )
        return

    transaction.on_commit(
        lambda: tasks.create_or_restore_offering_users_for_project.delay(
            project.uuid.hex
        )
    )


def create_offering_users_if_order_is_valid(
    sender, instance: Order, created=False, **kwargs
):
    """Create offering users for all project members when order reaches PENDING_PROVIDER or EXECUTING."""
    order = instance
    if not order.tracker.has_changed("state"):
        return
    if order.state not in [OrderStates.PENDING_PROVIDER, OrderStates.EXECUTING]:
        return
    if order.type != OrderTypes.CREATE:
        return

    offering = order.resource.offering
    if offering.type not in OFFERING_USER_ALLOWED_OFFERING_TYPES:
        return
    if not offering.plugin_options.get("service_provider_can_create_offering_user"):
        return

    project = order.project
    transaction.on_commit(
        lambda: tasks.create_or_restore_offering_users_for_project.delay(
            project.uuid.hex
        )
    )


def update_offering_user_username_after_offering_settings_change(
    sender, instance: Offering, created=False, **kwargs
):
    """
    Update offering user usernames after offering settings change.

    This handler is triggered when offering plugin_options change, which may affect
    username generation policies. It updates usernames for all OfferingUsers associated
    with the offering.

    Important: We call save() without update_fields to ensure state transition logic runs
    when username changes from empty to a valid value.
    """
    if created:
        return

    offering = instance

    if (
        offering.type not in OFFERING_USER_ALLOWED_OFFERING_TYPES
        or not offering.tracker.has_changed("plugin_options")
    ):
        return

    old_plugin_options = offering.tracker.previous("plugin_options") or {}
    new_plugin_options = offering.plugin_options or {}
    default_policy = utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
    old_policy = old_plugin_options.get("username_generation_policy", default_policy)
    new_policy = new_plugin_options.get("username_generation_policy", default_policy)

    if old_policy == new_policy:
        return

    offering_users = models.OfferingUser.objects.filter(
        offering=offering,
        state__in=[
            OfferingUserStates.CREATION_REQUESTED,
            OfferingUserStates.CREATING,
            OfferingUserStates.OK,
        ],
    )

    for offering_user in offering_users:
        new_username = utils.generate_username(offering_user.user, offering)
        old_username = offering_user.username
        logger.info(
            "OfferingUser username refresh after offering plugin_options change: offering_user_uuid=%s offering_uuid=%s old_username=%r new_username=%r affected_user_uuid=%s",
            offering_user.uuid.hex,
            offering.uuid.hex,
            old_username,
            new_username,
            offering_user.user.uuid.hex,
        )
        offering_user.username = new_username

        utils.setup_linux_related_data(offering_user, offering)
        # Call save() without update_fields to trigger state transition logic in OfferingUser.save()
        # This ensures state is updated from CREATION_REQUESTED to OK when username becomes available
        offering_user.save()


def update_offering_user_username_after_user_change(sender, instance: User, **kwargs):
    """
    Set new username for offering users after site_username in user details has been changed.

    This handler is triggered when user.details changes, specifically when site_username
    is updated for users using identity claim username generation policy.

    Important: We call save() without update_fields to ensure state transition logic runs
    when username changes from empty to a valid value.
    """
    user = instance

    # Update username for offering users only if site_username has been changed
    if not user.tracker.has_changed("details") or not user.details.get("site_username"):
        return

    offering_users = models.OfferingUser.objects.filter(
        user=user,
        offering__type__in=OFFERING_USER_ALLOWED_OFFERING_TYPES,
        offering__plugin_options__username_generation_policy=utils.UsernameGenerationPolicy.IDENTITY_CLAIM.value,
    )

    for offering_user in offering_users:
        offering = offering_user.offering
        old_username = offering_user.username
        new_username = utils.generate_username(user, offering)
        logger.info(
            "OfferingUser username refresh after user.details change: offering_user_uuid=%s offering_uuid=%s old_username=%r new_username=%r affected_user_uuid=%s",
            offering_user.uuid.hex,
            offering.uuid.hex,
            old_username,
            new_username,
            user.uuid.hex,
        )
        offering_user.username = new_username

        utils.setup_linux_related_data(offering_user, offering)
        # Call save() without update_fields to trigger state transition logic in OfferingUser.save()
        # This ensures state is updated from CREATION_REQUESTED to OK when username becomes available
        offering_user.save()


def update_offering_user_username_after_freeipa_profile_update(
    sender, instance: Profile, created=False, **kwargs
):
    """
    Update offering user usernames after FreeIPA profile creation/update.

    This handler is triggered when a user creates or updates their FreeIPA profile.
    It updates the username for all OfferingUsers that use FreeIPA username generation policy.

    Important: We call save() without update_fields to ensure the full save() method runs,
    which includes the state transition logic that sets state to OK when username is available.
    """
    profile = instance

    if not profile.tracker.has_changed("username") or not created:
        return

    offering_users = models.OfferingUser.objects.filter(
        user=profile.user,
        is_restricted=False,
        offering__plugin_options__username_generation_policy=utils.UsernameGenerationPolicy.FREEIPA.value,
    )

    for offering_user in offering_users:
        logger.info(
            "Updating %s username after FreeIPA profile %s change",
            offering_user,
            profile,
        )
        old_username = offering_user.username
        new_username = utils.generate_username(profile.user, offering_user.offering)

        logger.info(
            "OfferingUser username refresh after FreeIPA profile update: offering_user_uuid=%s offering_uuid=%s old_username=%r new_username=%r affected_user_uuid=%s",
            offering_user.uuid.hex,
            offering_user.offering.uuid.hex,
            old_username,
            new_username,
            profile.user.uuid.hex,
        )
        offering_user.username = new_username
        # Call save() without update_fields to trigger state transition logic in OfferingUser.save()
        # This ensures state is updated from CREATION_REQUESTED to OK when username becomes available
        offering_user.save()


def notify_user_about_rejected_order(sender, instance: Order, created=False, **kwargs):
    """Notify user about rejected order."""
    if created:
        return

    order = instance

    if order.completed_at is not None:
        return

    if (
        order.tracker.has_changed("state")
        and order.state in OrderStates.TERMINAL_STATES
    ):
        if order.state == OrderStates.REJECTED:
            tasks.notify_user_that_order_been_rejected.delay(order.uuid.hex)


def manage_maintenance_admin_announcements(sender, instance, created, **kwargs):
    """
    Manage AdminAnnouncement lifecycle based on MaintenanceAnnouncement state changes.

    Handles:
    - Creation when DRAFT → SCHEDULED
    - Cleanup when SCHEDULED → DRAFT (unschedule)
    - Cleanup when → CANCELLED
    - Refresh content/timing on SCHEDULED → IN_PROGRESS
    - Collapse window on IN_PROGRESS → COMPLETED
    - Update content when maintenance or affected offerings change
    """

    # Get previous state to detect transitions
    if not created and instance.tracker.has_changed("state"):
        old_state = instance.tracker.previous("state")
        new_state = instance.state

        # DRAFT → SCHEDULED: Create AdminAnnouncement
        if (
            old_state == MaintenanceState.DRAFT
            and new_state == MaintenanceState.SCHEDULED
        ):
            _create_maintenance_announcement(instance)

        # SCHEDULED → DRAFT: Remove AdminAnnouncement (unscheduled)
        elif (
            old_state == MaintenanceState.SCHEDULED
            and new_state == MaintenanceState.DRAFT
        ):
            _cleanup_maintenance_announcement(instance)

        # → CANCELLED: Remove AdminAnnouncement
        elif new_state == MaintenanceState.CANCELLED:
            _cleanup_maintenance_announcement(instance)

        # SCHEDULED → IN_PROGRESS: refresh content/window to use actual_start
        elif (
            old_state == MaintenanceState.SCHEDULED
            and new_state == MaintenanceState.IN_PROGRESS
            and _check_and_handle_missing_admin_announcement(instance)
        ):
            _update_maintenance_announcement_content(instance)

        # IN_PROGRESS → COMPLETED: collapse window to actual_end + buffer
        elif (
            old_state == MaintenanceState.IN_PROGRESS
            and new_state == MaintenanceState.COMPLETED
            and _check_and_handle_missing_admin_announcement(instance)
        ):
            _collapse_announcement_on_completion(instance)

    # Handle content/timing updates while maintenance is scheduled or in progress
    elif (
        not created
        and instance.state in (MaintenanceState.SCHEDULED, MaintenanceState.IN_PROGRESS)
        and _has_content_changes(instance)
        and _check_and_handle_missing_admin_announcement(instance)
    ):
        _update_maintenance_announcement_content(instance)


def _compute_announcement_window(maintenance):
    """Compute (active_from, active_to) for the announcement.

    Prefers actual_start/actual_end when set so an overrun or early finish is
    reflected. Honours the trailing buffer Constance setting so the banner
    stays visible briefly after completion.
    """
    notify_before_minutes = config.MAINTENANCE_ANNOUNCEMENT_NOTIFY_BEFORE_MINUTES
    trailing_buffer_minutes = int(
        config.MAINTENANCE_ANNOUNCEMENT_TRAILING_BUFFER_MINUTES
    )

    start_reference = maintenance.actual_start or maintenance.scheduled_start
    active_from = start_reference - timezone.timedelta(minutes=notify_before_minutes)

    effective_end = maintenance.actual_end or maintenance.scheduled_end
    # During an overrun the scheduled_end may be in the past; keep the banner
    # visible until now() + buffer so it doesn't disappear before the operator
    # patches scheduled_end.
    if (
        maintenance.state == MaintenanceState.IN_PROGRESS
        and not maintenance.actual_end
        and timezone.now() > effective_end
    ):
        effective_end = max(effective_end, timezone.now())
    active_to = effective_end + timezone.timedelta(minutes=trailing_buffer_minutes)

    return active_from, active_to


def _create_maintenance_announcement(maintenance):
    """Create AdminAnnouncement with rich markdown content."""

    # Delete any existing announcement first (defensive)
    if maintenance.admin_announcement:
        maintenance.admin_announcement.delete()

    # Generate content
    content = MaintenanceAnnouncementTemplate.generate_announcement_content(maintenance)
    announcement_type = MaintenanceAnnouncementTemplate.get_announcement_priority(
        maintenance
    )

    active_from, active_to = _compute_announcement_window(maintenance)

    # Create announcement
    admin_announcement = AdminAnnouncement.objects.create(
        description=content,
        type=announcement_type,
        active_from=active_from,
        active_to=active_to,
    )

    _update_admin_announcement_reference(maintenance, admin_announcement)


def _cleanup_maintenance_announcement(maintenance):
    """Remove associated AdminAnnouncement."""
    if maintenance.admin_announcement:
        maintenance.admin_announcement.delete()
        _update_admin_announcement_reference(maintenance, None)


def _update_maintenance_announcement_content(maintenance):
    """Update AdminAnnouncement content and window from current maintenance state."""

    if not maintenance.admin_announcement:
        return

    try:
        # Regenerate content
        content = MaintenanceAnnouncementTemplate.generate_announcement_content(
            maintenance
        )
        announcement_type = MaintenanceAnnouncementTemplate.get_announcement_priority(
            maintenance
        )

        active_from, active_to = _compute_announcement_window(maintenance)

        maintenance.admin_announcement.description = content
        maintenance.admin_announcement.type = announcement_type
        maintenance.admin_announcement.active_from = active_from
        maintenance.admin_announcement.active_to = active_to
        maintenance.admin_announcement.save()

    except AdminAnnouncement.DoesNotExist:
        # AdminAnnouncement was manually deleted
        _clear_admin_announcement_reference(maintenance)


def _collapse_announcement_on_completion(maintenance):
    """Recompute window on completion so the banner clears after the buffer."""
    _update_maintenance_announcement_content(maintenance)


def _has_content_changes(maintenance):
    """Check if maintenance has changes that affect announcement content or window."""
    content_fields = [
        "name",
        "message",
        "scheduled_start",
        "scheduled_end",
        "actual_start",
        "actual_end",
        "maintenance_type",
    ]
    return any(maintenance.tracker.has_changed(field) for field in content_fields)


def _update_admin_announcement_reference(maintenance, admin_announcement=None):
    """
    Update the AdminAnnouncement reference without triggering signals.

    Args:
        maintenance: MaintenanceAnnouncement instance
        admin_announcement: AdminAnnouncement instance or None to clear
    """
    # Update using direct SQL to avoid triggering signals and potential recursion
    maintenance.__class__.objects.filter(pk=maintenance.pk).update(
        admin_announcement=admin_announcement
    )
    maintenance.admin_announcement = admin_announcement


def _clear_admin_announcement_reference(maintenance, reason="was manually deleted"):
    """
    Clear the AdminAnnouncement reference from maintenance.

    Args:
        maintenance: MaintenanceAnnouncement instance
        reason: Reason for clearing (used in log message)
    """
    logger.warning(
        f"AdminAnnouncement for maintenance {maintenance.uuid} {reason}. "
        f"Clearing reference and not regenerating."
    )
    _update_admin_announcement_reference(maintenance, None)


def _check_and_handle_missing_admin_announcement(maintenance, action="update"):
    """
    Check if AdminAnnouncement still exists and handle if missing.

    Args:
        maintenance: MaintenanceAnnouncement instance
        action: Action being performed (used in log message)

    Returns:
        bool: True if AdminAnnouncement exists, False if it was cleared
    """
    if not maintenance.admin_announcement_id:
        return False

    if not AdminAnnouncement.objects.filter(
        id=maintenance.admin_announcement_id
    ).exists():
        reason_msg = (
            "was manually deleted. Not regenerating unless maintenance is updated"
        )
        if action == "offering_change":
            reason_msg = (
                "was manually deleted. Not regenerating unless maintenance is updated"
            )

        _clear_admin_announcement_reference(maintenance, reason_msg)
        return False

    return True


def update_maintenance_announcement_on_offering_change(sender, instance, **kwargs):
    """Update AdminAnnouncement when affected offerings change."""

    maintenance = instance.maintenance
    if maintenance.state in (
        MaintenanceState.SCHEDULED,
        MaintenanceState.IN_PROGRESS,
    ) and _check_and_handle_missing_admin_announcement(maintenance, "offering_change"):
        _update_maintenance_announcement_content(maintenance)


def cleanup_admin_announcement_on_maintenance_deletion(sender, instance, **kwargs):
    """Ensure AdminAnnouncement is cleaned up when MaintenanceAnnouncement is deleted."""
    if instance.admin_announcement:
        try:
            instance.admin_announcement.delete()
        except (ObjectDoesNotExist, AdminAnnouncement.DoesNotExist):
            # AdminAnnouncement was already deleted - this is fine
            logger.debug(
                f"AdminAnnouncement for maintenance {instance.uuid} was already deleted"
            )
        except Exception as e:
            # Log other unexpected exceptions but don't break the deletion process
            logger.warning(
                f"Failed to delete AdminAnnouncement for maintenance {instance.uuid}: {e}"
            )


def add_maintenance_fields_to_admin_announcement_serializer(sender, fields, **kwargs):
    """Add maintenance-related fields to AdminAnnouncementSerializer when maintenance is scheduled."""
    # Add maintenance fields if the AdminAnnouncement has a related MaintenanceAnnouncement
    fields["maintenance_uuid"] = serializers.SerializerMethodField()
    fields["maintenance_name"] = serializers.SerializerMethodField()
    fields["maintenance_type"] = serializers.SerializerMethodField()
    fields["maintenance_state"] = serializers.SerializerMethodField()
    fields["maintenance_scheduled_start"] = serializers.SerializerMethodField()
    fields["maintenance_scheduled_end"] = serializers.SerializerMethodField()
    fields["maintenance_service_provider"] = serializers.SerializerMethodField()
    fields["maintenance_affected_offerings"] = serializers.SerializerMethodField()
    fields["maintenance_external_reference_url"] = serializers.SerializerMethodField()

    # Add methods to the sender class (AdminAnnouncementSerializer)
    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_uuid(self, obj) -> str | None:
        try:
            return (
                str(obj.maintenance_announcement.uuid)
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_name(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.name
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_type(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.maintenance_type
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_state(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.state
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_maintenance_scheduled_start(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.scheduled_start
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_maintenance_scheduled_end(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.scheduled_end
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_service_provider(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.service_provider.name
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                and obj.maintenance_announcement.service_provider
                else None
            )
        except AttributeError:
            return None

    @extend_schema_field(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "uuid": {"type": "string", "format": "uuid"},
                    "name": {"type": "string"},
                    "impact_level": {"type": "string"},
                    "impact_level_display": {"type": "string"},
                    "impact_description": {"type": "string"},
                },
            },
        }
    )
    def get_maintenance_affected_offerings(self, obj) -> list[dict[str, Any]]:
        try:
            if (
                not hasattr(obj, "maintenance_announcement")
                or not obj.maintenance_announcement
            ):
                return []

            affected_offerings = []
            for (
                affected_offering
            ) in obj.maintenance_announcement.affected_offerings.all():
                offering_info = {
                    "uuid": str(affected_offering.offering.uuid),
                    "name": affected_offering.offering.name,
                    "impact_level": affected_offering.impact_level,
                    "impact_level_display": affected_offering.get_impact_level_display(),
                    "impact_description": affected_offering.impact_description,
                }
                affected_offerings.append(offering_info)

            return affected_offerings
        except AttributeError:
            return []

    @extend_schema_field(OpenApiTypes.STR)
    def get_maintenance_external_reference_url(self, obj) -> str | None:
        try:
            return (
                obj.maintenance_announcement.external_reference_url
                if hasattr(obj, "maintenance_announcement")
                and obj.maintenance_announcement
                else None
            )
        except AttributeError:
            return None

    # Add the methods to the serializer class
    sender.get_maintenance_uuid = get_maintenance_uuid
    sender.get_maintenance_name = get_maintenance_name
    sender.get_maintenance_type = get_maintenance_type
    sender.get_maintenance_state = get_maintenance_state
    sender.get_maintenance_scheduled_start = get_maintenance_scheduled_start
    sender.get_maintenance_scheduled_end = get_maintenance_scheduled_end
    sender.get_maintenance_service_provider = get_maintenance_service_provider
    sender.get_maintenance_affected_offerings = get_maintenance_affected_offerings
    sender.get_maintenance_external_reference_url = (
        get_maintenance_external_reference_url
    )


def close_course_accounts_after_project_removal(
    sender, instance: structure_models.Project, **kwargs
):
    if not settings.WALDUR_CORE.get("COURSE_ACCOUNT_USE_API"):
        return

    course_accounts = models.CourseAccount.objects.filter(project=instance)
    if not course_accounts.exists():
        return
    try:
        api_access_token = utils.get_course_account_api_token()
    except httpx.HTTPError:
        logger.error(
            "Unable to get course account API token, skipping accounts removal for project %s",
            instance,
        )
        return

    for course_account in course_accounts:
        try:
            utils.close_course_account(course_account, api_access_token)
        except httpx.HTTPError:
            logger.error(
                "Unable to close course account %s from project %s",
                course_account.user.username,
                instance,
            )


def log_terms_of_service_consent_granted(
    sender, instance: models.UserOfferingConsent, created=False, **kwargs
):
    """Log when a user grants consent to Terms of Service."""
    if not created:
        return
    event_logger.emit(
        "User {user_name} has accepted Terms of Service for offering {offering_name}, ToS version {version}.",
        event_type=EventType.TERMS_OF_SERVICE_CONSENT_GRANTED,
        event_context={
            "user": instance.user,
            "offering": instance.offering,
            "consent": instance,
            "version": instance.version,
            "user_name": instance.user.full_name or instance.user.username,
            "offering_name": instance.offering.name,
        },
        scopes=[instance.offering, instance.offering.customer],
    )


def log_terms_of_service_consent_revoked(
    sender, instance: models.UserOfferingConsent, created=False, **kwargs
):
    """Log when a user revokes consent to Terms of Service."""
    if created or not instance.tracker.has_changed("revocation_date"):
        return

    if instance.revocation_date is None:
        return

    event_logger.emit(
        "User {user_name} has revoked Terms of Service consent for offering {offering_name}, ToS version {version}.",
        event_type=EventType.TERMS_OF_SERVICE_CONSENT_REVOKED,
        event_context={
            "user": instance.user,
            "offering": instance.offering,
            "consent": instance,
            "version": instance.version,
            "user_name": instance.user.full_name or instance.user.username,
            "offering_name": instance.offering.name,
            "revocation_date": instance.revocation_date,
        },
        scopes=[instance.offering, instance.offering.customer],
    )


def send_offering_user_created_message(
    sender, instance: models.OfferingUser, created=False, **kwargs
):
    """Send OfferingUser creation message to message queue for external systems."""
    if not created:
        return

    # Skip message sending if disabled via context (e.g., during imports)
    if get_skip_side_effects():
        return

    offering_user = instance
    offering = offering_user.offering

    exposed_attrs = set(
        models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(offering)
    )
    attributes = _build_filtered_user_attributes(offering_user.user, exposed_attrs)

    payload = {
        "offering_user_uuid": offering_user.uuid.hex,
        "user_uuid": offering_user.user.uuid.hex,
        "username": offering_user.username,
        "state": offering_user.get_state_display(),
        "runtime_state": offering_user.runtime_state,
        "action": "create",
        "attributes": attributes,
    }

    logger.debug("Preparing OfferingUser creation messages for %s", offering_user)

    messages = marketplace_utils.prepare_messages(
        offering,
        payload,
        ObservableObjectType.OFFERING_USER,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def send_offering_user_updated_message(
    sender, instance: models.OfferingUser, created=False, **kwargs
):
    """Send OfferingUser update message to message queue for external systems."""
    if get_skip_side_effects():
        return
    if created:
        return

    offering_user = instance
    offering = offering_user.offering

    changed_fields = offering_user.tracker.changed()
    if not changed_fields:
        return

    payload = {
        "offering_user_uuid": offering_user.uuid.hex,
        "user_uuid": offering_user.user.uuid.hex,
        "username": offering_user.username,
        "state": offering_user.get_state_display(),
        "runtime_state": offering_user.runtime_state,
        "changed_fields": list(changed_fields.keys()),
        "action": "update",
    }

    logger.debug("Preparing OfferingUser update message for %s", offering_user)

    messages = marketplace_utils.prepare_messages(
        offering,
        payload,
        ObservableObjectType.OFFERING_USER,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


def send_offering_user_deleted_message(sender, instance: models.OfferingUser, **kwargs):
    """Send OfferingUser deletion message to message queue for external systems."""
    if get_skip_side_effects():
        return
    offering_user = instance
    offering = offering_user.offering

    payload = {
        "offering_user_uuid": offering_user.uuid.hex,
        "user_uuid": offering_user.user.uuid.hex,
        "username": offering_user.username,
        "action": "delete",
    }

    logger.debug("Preparing OfferingUser deletion message for %s", offering_user)

    messages = marketplace_utils.prepare_messages(
        offering,
        payload,
        ObservableObjectType.OFFERING_USER,
    )
    if messages:
        logging_tasks.publish_messages.delay(messages)


USER_FIELD_TO_ATTRIBUTE = marketplace_utils.USER_FIELD_TO_ATTRIBUTE


def _serialize_user_field(value):
    """Serialize a User model field value for JSON payload."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _build_filtered_user_attributes(user, exposed_attributes):
    """Build dict of user profile values filtered by exposed attributes."""
    attributes = {}
    for field, attr_gate in USER_FIELD_TO_ATTRIBUTE.items():
        if attr_gate in exposed_attributes:
            val = getattr(user, field, None)
            if val is not None and val != "" and val != []:
                attributes[field] = _serialize_user_field(val)
    return attributes


def send_user_attribute_update_message(sender, instance, created=False, **kwargs):
    """Publish OFFERING_USER events when User profile attributes change.

    For each offering the user belongs to, filters changed attributes through
    OfferingUserAttributeConfig and publishes only exposed attribute values.
    """
    if created or get_skip_side_effects():
        return

    user = instance
    changed = user.tracker.changed()
    if not changed:
        return

    changed_profile_fields = set(changed.keys()) & set(USER_FIELD_TO_ATTRIBUTE.keys())
    if not changed_profile_fields:
        return

    for offering_user in models.OfferingUser.objects.filter(user=user).select_related(
        "offering"
    ):
        offering = offering_user.offering
        exposed_attrs = set(
            models.OfferingUserAttributeConfig.get_exposed_fields_for_offering(offering)
        )

        relevant_fields = {
            f
            for f in changed_profile_fields
            if USER_FIELD_TO_ATTRIBUTE[f] in exposed_attrs
        }
        if not relevant_fields:
            continue

        attributes = _build_filtered_user_attributes(user, exposed_attrs)

        payload = {
            "offering_user_uuid": offering_user.uuid.hex,
            "user_uuid": user.uuid.hex,
            "username": offering_user.username,
            "action": "attribute_update",
            "changed_attributes": sorted(relevant_fields),
            "attributes": attributes,
        }

        messages = marketplace_utils.prepare_messages(
            offering, payload, ObservableObjectType.OFFERING_USER
        )
        if messages:
            logging_tasks.publish_messages.delay(messages)


def notify_users_about_tos_update_signal(sender, instance, created, **kwargs):
    """Notify users when ToS is updated and requires re-consent."""

    if not config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
        return

    tos_config = instance

    if not tos_config.is_active or not tos_config.requires_reconsent:
        return

    if created:
        previous_tos = (
            models.OfferingTermsOfService.objects.filter(offering=tos_config.offering)
            .exclude(uuid=tos_config.uuid)
            .order_by("-created")
            .first()
        )
        old_version = previous_tos.version if previous_tos else ""
        new_version = tos_config.version
    else:
        if "version" not in tos_config.tracker.changed():
            return
        old_version = tos_config.tracker.previous("version") or ""
        new_version = tos_config.version

    transaction.on_commit(
        lambda: tasks.send_tos_reconsent_notification.delay(
            tos_config.offering.uuid, old_version, new_version
        )
    )


def notify_offering_user_about_tos_requirement(sender, instance, created, **kwargs):
    """Notify user about ToS requirement when OfferingUser is created."""

    if not config.ENFORCE_USER_CONSENT_FOR_OFFERINGS:
        return

    if not created:
        return

    offering_user = instance
    user = offering_user.user
    offering = offering_user.offering

    if user.is_staff or user.is_support:
        return

    if not offering.has_terms_of_service():
        return

    if not offering.plugin_options.get(
        "service_provider_can_create_offering_user", False
    ):
        return

    if offering.check_user_consent(user):
        return

    transaction.on_commit(
        lambda u=user.uuid, o=offering.uuid: tasks.send_tos_consent_notification.delay(
            o, u
        )
    )


def process_billing_on_resource_save(
    sender, instance: models.Resource, created=False, **kwargs
):
    """
    Handle resource state changes and billing events.
    """
    resource = instance
    if created:
        return

    tracker = resource.tracker

    if (
        resource.state == ResourceStates.OK
        and tracker.previous("state") == ResourceStates.CREATING
    ):
        MarketplaceBillingService.handle_resource_creation(resource)

    if (
        resource.state == ResourceStates.TERMINATED
        and tracker.previous("state") == ResourceStates.TERMINATING
    ):
        MarketplaceBillingService.handle_resource_termination(resource)

    if resource.state != ResourceStates.CREATING and tracker.has_changed("plan_id"):
        MarketplaceBillingService.handle_plan_change(resource)

    if resource.state != ResourceStates.CREATING and tracker.has_changed("limits"):
        MarketplaceBillingService.handle_limits_change(resource)


def update_resource_scope_availability_on_offering_state_change(
    sender, instance: Offering, created=False, **kwargs
):
    if created:
        return

    offering = instance

    if not offering.tracker.has_changed("state"):
        return

    if offering.state == OfferingStates.UNAVAILABLE:
        can_be_managed = False
    elif offering.tracker.previous("state") == OfferingStates.UNAVAILABLE:
        can_be_managed = True
    else:
        return

    tasks.update_resource_scope_availability.delay(offering.uuid.hex, can_be_managed)


def trigger_scim_sync_on_offering_endpoint_change(
    sender, instance: models.OfferingAccessEndpoint, created=False, **kwargs
):
    """Trigger SCIM entitlements synchronization when offering SSH endpoints change."""
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED or not scim_tasks.is_scim_configured():
        return

    if not instance.url or not instance.url.startswith("ssh://"):
        return

    scim_tasks.sync_users_for_offering_endpoint.delay(instance.offering.uuid.hex)


def trigger_scim_sync_on_offering_user_ok(
    sender, instance: OfferingUser, created=False, **kwargs
):
    """Trigger SCIM entitlements synchronization when OfferingUser transitions to OK with username."""
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED or not scim_tasks.is_scim_configured():
        return

    if created or not instance.tracker.has_changed("state"):
        return

    if instance.state != OfferingUserStates.OK or not instance.username:
        return

    transaction.on_commit(
        lambda: scim_tasks.sync_user_entitlements.delay(instance.user.uuid.hex)
    )


def trigger_scim_sync_on_resource_ok(
    sender, instance: Resource, created=False, **kwargs
):
    """Trigger SCIM entitlements synchronization when resource transitions to OK."""
    if not config.SCIM_MEMBERSHIP_SYNC_ENABLED or not scim_tasks.is_scim_configured():
        return

    if created or not instance.tracker.has_changed("state"):
        return

    if instance.state != ResourceStates.OK:
        return

    transaction.on_commit(
        lambda: scim_tasks.sync_users_for_offering_endpoint.delay(
            instance.offering.uuid.hex
        )
    )


def handle_user_role_revoked(
    sender, instance, current_user=None, reason=None, **kwargs
):
    """
    Handle user role revocation by removing users from robot accounts
    when they lose project membership.

    Args:
        sender: The model class that sent the signal
        instance: The UserRole instance that was revoked
        current_user: User who performed the revocation (optional)
        reason: Reason for revocation (optional)
        **kwargs: Additional keyword arguments
    """
    logger.info(
        f"Processing role revocation for user {instance.user} in scope {instance.scope}"
    )

    # Schedule the task to remove users from robot accounts
    # We use a Celery task to avoid blocking the signal handler
    # and to ensure proper transaction handling
    transaction.on_commit(
        lambda: remove_users_from_robot_accounts_on_permission_loss.delay(instance.id)
    )


def purge_offering_role_groups_on_scope_delete(sender, instance, **kwargs):
    """Drop OfferingRoleGroup rows that pointed at a now-deleted scope.

    The scope is a Resource or ResourceProject; both are referenced via
    a GenericForeignKey on OfferingRoleGroup, so cascade-delete from
    Django ORM does NOT fire automatically. Without this handler the
    rows would dangle and the gid allocator would skip those numbers
    forever.
    """
    from waldur_mastermind.marketplace import models as marketplace_models

    ct = ContentType.objects.get_for_model(sender)
    marketplace_models.OfferingRoleGroup.objects.filter(
        content_type=ct, object_id=instance.pk
    ).delete()


def trigger_user_action_recalculation_on_order_state_change(
    sender, instance: Order, created=False, **kwargs
):
    """
    Trigger immediate UserAction recalculation when Order state changes.

    This ensures that when an order is approved, rejected, cancelled, or completed,
    the related UserActions are immediately cleaned up for all affected users
    rather than waiting for the next periodic update.
    """
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    previous_state = instance.tracker.previous("state")
    current_state = instance.state

    # Only trigger recalculation when transitioning out of pending states
    pending_states = (
        OrderStates.PENDING_CONSUMER,
        OrderStates.PENDING_PROVIDER,
        OrderStates.PENDING_PROJECT,
        OrderStates.PENDING_START_DATE,
    )

    if previous_state not in pending_states:
        return

    logger.info(
        f"Order {instance.uuid} state changed from {previous_state} to {current_state}, "
        "triggering UserAction recalculation"
    )

    # Import here to avoid circular imports
    from waldur_core.user_actions.tasks import update_actions_for_provider

    # Schedule recalculation for the pending_order provider
    transaction.on_commit(lambda: update_actions_for_provider.delay("pending_order"))


def reconcile_offering_profile_on_roles_changed(
    sender, instance, action, pk_set, **kwargs
):
    """When OfferingProfile.roles M2M changes, schedule reconciliation
    across every offering bound to this profile."""
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    transaction.on_commit(
        lambda: tasks.reconcile_offering_profile_availabilities.delay(instance.id)
    )


def reconcile_offering_profile_on_offering_changed(sender, instance, **kwargs):
    """When an Offering is saved, schedule a reconciliation task. Cheap
    no-op when nothing has changed; covers profile FK changes without
    the fragility of FieldTracker post-save semantics."""
    transaction.on_commit(
        lambda: tasks.reconcile_offering_availabilities.delay(instance.id)
    )


def log_resource_limit_change_request_events(sender, instance, created=False, **kwargs):
    """Log events when resource limit change request is created or reviewed."""
    event_context = {
        "resource_limit_change_request": instance,
        "resource": instance.resource,
    }
    if created:
        transaction.on_commit(
            lambda: tasks.send_resource_limit_change_request_notification.delay(
                instance.uuid.hex
            )
        )
        event_logger.emit(
            "Resource limit change request has been created.",
            event_type=EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_CREATED,
            event_context=event_context,
            scopes=[instance.resource],
        )
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state == ReviewStates.APPROVED:
        transaction.on_commit(
            lambda: (
                tasks.send_resource_limit_change_request_approved_notification.delay(
                    instance.uuid.hex
                )
            )
        )
        event_logger.emit(
            "Resource limit change request has been approved.",
            event_type=EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_APPROVED,
            event_context=event_context,
            scopes=[instance.resource],
        )
    elif instance.state == ReviewStates.REJECTED:
        transaction.on_commit(
            lambda: (
                tasks.send_resource_limit_change_request_rejected_notification.delay(
                    instance.uuid.hex
                )
            )
        )
        event_logger.emit(
            "Resource limit change request has been rejected.",
            event_type=EventType.MARKETPLACE_RESOURCE_LIMIT_CHANGE_REQUEST_REJECTED,
            event_context=event_context,
            scopes=[instance.resource],
        )


def release_posix_allocations_on_consumer_deletion(sender, instance, **kwargs):
    """Mark the deleted POSIX id consumer's identity as released.

    A released value is recycled automatically on the next allocation from the
    same pool and namespace; the released row is kept as an audit trail. Note:
    OfferingUser also has a soft DELETED lifecycle state — release is
    intentionally tied to actual row deletion only.
    """
    posix_ids.release_posix_allocations(instance)
