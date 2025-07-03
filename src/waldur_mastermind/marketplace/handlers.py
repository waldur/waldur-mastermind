import logging
from decimal import Decimal

import httpx
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import signals
from django.utils.timezone import now

from waldur_core.core import utils as core_utils
from waldur_core.core.log import event_logger
from waldur_core.core.models import User
from waldur_core.structure import models as structure_models
from waldur_core.structure.models import Customer, Project
from waldur_core.users import models as users_models
from waldur_core.users.tasks import process_invitation
from waldur_freeipa.models import Profile
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.models import (
    Offering,
    OfferingComponent,
    OfferingUser,
    OfferingUserRole,
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
from waldur_mastermind.marketplace_script import PLUGIN_NAME as SCRIPT_PLUGIN_NAME
from waldur_mastermind.marketplace_site_agent import (
    PLUGIN_NAME as SITE_AGENT_PLUGIN_NAME,
)

from . import PLUGIN_NAME, callbacks, log, models, tasks, utils

logger = logging.getLogger(__name__)

OFFERING_USER_ALLOWED_OFFERING_TYPES = [
    PLUGIN_NAME,
    SITE_AGENT_PLUGIN_NAME,
    SCRIPT_PLUGIN_NAME,
]

ROBOT_ACCOUNT_TYPE = "Robot account"
SERVICE_ACCOUNT_TYPE = "Service account"


def create_screenshot_thumbnail(sender, instance: Screenshot, created=False, **kwargs):
    if not created:
        return

    transaction.on_commit(
        lambda: tasks.create_screenshot_thumbnail.delay(instance.uuid)
    )


def log_order_events(sender, instance: Order, created=False, **kwargs):
    order: models.Order = instance
    if created:
        if order.state not in (
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
        ):
            # Skip logging for imported order
            return
        if not order.resource:
            return
        if order.type == models.Order.Types.TERMINATE:
            log.log_resource_terminate_requested(order.resource)
        elif order.type == models.Order.Types.UPDATE:
            log.log_resource_update_requested(order.resource)
    else:
        if not order.tracker.has_changed("state"):
            return
        if order.state == OrderStates.EXECUTING:
            log.log_order_approved(order)
        elif order.state == OrderStates.REJECTED:
            log.log_order_rejected(order)
        elif order.state == OrderStates.DONE:
            log.log_order_completed(order)
        elif order.state == OrderStates.CANCELED:
            log.log_order_canceled(order)
        elif order.state == OrderStates.ERRED:
            log.log_order_failed(order)


def log_resource_events(sender, instance: Resource, created=False, **kwargs):
    resource = instance
    # Skip logging for imported resource
    if created and instance.state == ResourceStates.CREATING:
        log.log_resource_creation_requested(resource)


def init_resource_parent(sender, instance: Resource, created=False, **kwargs):
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


def close_service_accounts_on_project_deletion(sender, instance: Project, **kwargs):
    project: structure_models.Project = instance

    service_accounts = models.ProjectServiceAccount.objects.filter(project=project)
    if not service_accounts.exists():
        return

    for service_account in service_accounts:
        try:
            utils.delete_service_account(service_account)
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                "Failed to request deletion of service account %s for project %s: %s",
                service_account,
                project,
                exc,
            )
            continue


def close_customer_service_accounts_on_customer_deletion(
    sender, instance: Customer, **kwargs
):
    customer: structure_models.Customer = instance
    service_accounts = models.CustomerServiceAccount.objects.filter(customer=customer)
    if not service_accounts.exists():
        return
    for service_account in service_accounts:
        try:
            utils.delete_service_account(service_account)
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                "Failed to request deletion of service account %s for customer %s: %s",
                service_account,
                customer,
                exc,
            )
            continue


def process_invitations_and_orders_when_project_start_date_is_unset(
    sender, instance: Project, created=False, **kwargs
):
    if created:
        return

    project = instance

    if not project.tracker.has_changed("start_date"):
        return

    if project.start_date:
        return

    project_content_type = ContentType.objects.get_for_model(structure_models.Project)
    invitations = users_models.Invitation.objects.filter(
        state=users_models.Invitation.State.PENDING_PROJECT,
        object_id=project.id,
        content_type=project_content_type,
    )
    for invitation in invitations:
        invitation.state = models.Invitation.State.PENDING
        invitation.save()
        sender = invitation.created_by.full_name or invitation.created_by.username
        transaction.on_commit(
            lambda: process_invitation.delay(invitation.uuid.hex, sender)
        )

    orders = models.Order.objects.filter(
        state=OrderStates.PENDING_PROJECT, project=project
    )
    for order in orders:
        # Setting the state to PENDING_PROVIDER because direct transition
        # from PENDING_PROJECT to EXECUTING is not supported
        order.state = OrderStates.PENDING_PROVIDER
        order.save(update_fields=["state"])
        if utils.order_should_not_be_reviewed_by_provider(order):
            order.set_state_executing()
            order.save(update_fields=["state"])
            transaction.on_commit(
                lambda: tasks.process_order_on_commit(order, order.created_by)
            )
        else:
            transaction.on_commit(
                lambda: tasks.notify_provider_about_pending_order.delay(order.uuid)
            )


def update_resource_when_order_is_rejected_or_erred(
    sender, instance: Order, created=False, **kwargs
):
    order: models.Order = instance
    if not order.tracker.has_changed("state"):
        return
    resource = order.resource
    if order.state == OrderStates.REJECTED:
        if order.type == models.Order.Types.CREATE:
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
    order: models.Order = instance
    if order.type != models.Order.Types.CREATE:
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
    if instance.state == OfferingStates.ACTIVE:
        instance.category.add_quota_usage("offering_count", -1)


def update_category_offerings_count(sender, **kwargs):
    for category in models.Category.objects.all():
        value = models.Offering.objects.filter(
            category=category, state=OfferingStates.ACTIVE
        ).count()
        category.set_quota_usage("offering_count", value)


def delete_service_setting_when_offering_is_deleted(
    sender, instance: Offering, **kwargs
):
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


def switch_resource_plan_period_when_plan_is_updated(
    sender, instance: Resource, created=False, **kwargs
):
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


def update_or_create_quotas(resource):
    components_map = resource.offering.get_limit_components()
    for key, value in resource.limits.items():
        component = components_map.get(key)
        if component:
            models.ComponentQuota.objects.update_or_create(
                resource=resource, component=component, defaults={"limit": value}
            )


def sync_limits(sender, instance: Resource, created=False, **kwargs):
    if not created and not instance.tracker.has_changed("limits"):
        return
    transaction.on_commit(lambda: update_or_create_quotas(instance))


@transaction.atomic()
def limit_update_succeeded(sender, order: models.Order, **kwargs):
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


def limit_update_failed(sender, order, error_message, **kwargs):
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
    log.log_resource_limit_update_failed(resource)


def update_customer_of_offering_if_project_has_been_moved(
    sender, project, old_customer, new_customer, **kwargs
):
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
    if created:
        return

    if instance.tracker.has_changed("price"):
        event_logger.info(
            f"Current price of component {instance.component.type} in plan {instance.plan.name} has been updated.",
            event_type="marketplace_plan_component_current_price_updated",
            event_context={
                "plan_component": instance,
                "old_value": instance.tracker.previous("price"),
                "new_value": Decimal(instance.price)
                if isinstance(instance.price, str)
                else instance.price,
            },
            group="marketplace_plan_component",
        )
    if instance.tracker.has_changed("future_price"):
        event_logger.info(
            f"Future price of component {instance.component.type} in plan {instance.plan.name} has been updated.",
            event_type="marketplace_plan_component_future_price_updated",
            event_context={
                "plan_component": instance,
                "old_value": instance.tracker.previous("future_price"),
                "new_value": Decimal(instance.future_price)
                if isinstance(instance.future_price, str)
                else instance.future_price,
            },
            group="marketplace_plan_component",
        )
    if instance.tracker.has_changed("amount"):
        event_logger.info(
            f"Quota of component {instance.component.type} in plan {instance.plan.name} has been updated.",
            event_type="marketplace_plan_component_quota_updated",
            event_context={
                "plan_component": instance,
                "old_value": instance.tracker.previous("amount"),
                "new_value": Decimal(instance.amount)
                if isinstance(instance.amount, str)
                else instance.amount,
            },
            group="marketplace_plan_component",
        )


def offering_component_has_been_created_or_updated(
    sender, instance: OfferingComponent, created=False, **kwargs
):
    if created:
        event_logger.info(
            f"Offering component {instance.name} has been created.",
            event_type="marketplace_offering_component_created",
            event_context={
                "offering_component": instance,
            },
            group="marketplace_offering_component",
        )
    else:
        changes = [
            f"{field}: {instance.tracker.previous(field)} -> {getattr(instance, field, None)}"
            for field in instance.tracker.changed()
        ]
        if changes:
            diff = ", ".join(changes)
            event_logger.info(
                f"Offering component {instance.name} has been updated. Details: {diff}.",
                event_type="marketplace_offering_component_updated",
                event_context={
                    "offering_component": instance,
                },
                group="marketplace_offering_component",
            )


def offering_component_has_been_deleted(sender, instance: OfferingComponent, **kwargs):
    event_logger.info(
        f"Offering component {instance.name} has been deleted.",
        event_type="marketplace_offering_component_deleted",
        event_context={
            "offering_component": instance,
        },
        group="marketplace_offering_component",
    )


def plan_has_been_created_or_updated(sender, instance: Plan, created=False, **kwargs):
    if created:
        event_logger.info(
            f"Plan {instance.name} has been created.",
            event_type="marketplace_plan_created",
            event_context={
                "plan": instance,
            },
            group="marketplace_plan",
        )
    else:
        if instance.tracker.has_changed("archived"):
            event_logger.info(
                f"Plan {instance.name} has been archived.",
                event_type="marketplace_plan_archived",
                event_context={
                    "plan": instance,
                },
                group="marketplace_plan",
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
                event_logger.info(
                    f"Plan {instance.name} has been updated. Details: {diff}.",
                    event_type="marketplace_plan_updated",
                    event_context={
                        "plan": instance,
                    },
                    group="marketplace_plan",
                )


def offering_has_been_created_or_updated(
    sender, instance: Offering, created=False, **kwargs
):
    if created:
        event_logger.info(
            "Offering has been created.",
            event_type="marketplace_offering_created",
            event_context={
                "offering": instance,
            },
            group="marketplace_offering",
        )
    else:
        if instance.tracker.has_changed("state"):
            event_logger.info(
                "Offering state has been updated.",
                event_type="marketplace_offering_updated",
                event_context={
                    "offering": instance,
                    "old_value": models.Offering(
                        state=instance.tracker.previous("state")
                    ).get_state_display(),
                    "new_value": instance.get_state_display(),
                },
                group="marketplace_offering",
            )


def resource_has_been_changed(sender, instance: Resource, created=False, **kwargs):
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
        if field == "state":
            old_value_display = models.Resource.get_state_display(
                models.Resource(state=old_value)
            )
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
            if old_value == new_value:
                continue

        if not old_value and not new_value:
            continue
        changed.append({"name": field, "from": old_value, "to": new_value})

    log.log_resource_update_succeeded(instance, changed)


def resource_state_has_been_changed(
    sender, instance: Resource, created=False, **kwargs
):
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
    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    if instance.state != ResourceStates.TERMINATED:
        return

    project = instance.project

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
            event_logger.info(
                "Project {project_name} is going to be deleted because end date has been reached and there are no active resources.",
                event_type="project_deletion_triggered",
                event_context={"project": project},
                group="project",
            )
            project.delete()


def log_offering_user_created(sender, instance: OfferingUser, created=False, **kwargs):
    if not created:
        return
    log.log_offering_user_created(instance)


def log_offering_user_deleted(sender, instance: OfferingUser, **kwargs):
    log.log_offering_user_deleted(instance)


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
    if not created:
        changed_string = generate_changes_string(
            instance.tracker.changed(), instance, SERVICE_ACCOUNT_TYPE
        )
        event_logger.info(
            changed_string,
            event_type="service_account_updated",
            event_context={"service_account": instance},
            group="marketplace_service_account",
        )
        return
    event_logger.info(
        "Service account {service_account_username} has been created.",
        event_type="service_account_created",
        event_context={"service_account": instance},
        group="marketplace_service_account",
    )


def log_service_account_deleted(sender, instance: ScopedServiceAccount, **kwargs):
    event_logger.info(
        "Service account {service_account_username} has been deleted.",
        event_type="service_account_deleted",
        event_context={"service_account": instance},
        group="marketplace_service_account",
    )


def log_resource_robot_account_created_or_updated(
    sender, instance: RobotAccount, created=False, **kwargs
):
    if not created:
        changed_string = generate_changes_string(
            instance.tracker.changed(), instance, ROBOT_ACCOUNT_TYPE
        )
        event_logger.info(
            changed_string,
            event_type="resource_robot_account_updated",
            event_context={"robot_account": instance},
            group="marketplace_robot_account",
        )
        return
    event_logger.info(
        "Robot account {robot_account_username} has been created.",
        event_type="resource_robot_account_created",
        event_context={"robot_account": instance},
        group="marketplace_robot_account",
    )


def log_resource_robot_account_deleted(sender, instance: RobotAccount, **kwargs):
    event_logger.info(
        "Robot account {robot_account_username} has been deleted.",
        event_type="resource_robot_account_deleted",
        event_context={"robot_account": instance},
        group="marketplace_robot_account",
    )


def create_offering_users_when_project_role_granted(sender, instance, **kwargs):
    if not isinstance(instance.scope, structure_models.Project):
        return
    project = instance.scope
    user = instance.user
    resources = project.resource_set.filter(
        state=ResourceStates.OK,
        offering__type__in=OFFERING_USER_ALLOWED_OFFERING_TYPES,
    )
    offering_ids = set(resources.values_list("offering_id", flat=True))
    offerings = models.Offering.objects.filter(id__in=offering_ids)

    for offering in offerings:
        if not offering.plugin_options.get("service_provider_can_create_offering_user"):
            logger.info(
                "It is not allowed to create users for current offering %s.", offering
            )
            continue

        if models.OfferingUser.objects.filter(
            offering=offering,
            user=user,
        ).exists():
            logger.info("An offering user for %s in %s already exists", user, offering)
            continue

        username = utils.generate_username(user, offering)

        offering_user = models.OfferingUser.objects.create(
            offering=offering,
            user=user,
            username=username,
        )
        utils.setup_linux_related_data(offering_user, offering)
        offering_user.save(update_fields=["backend_metadata"])


def create_offering_user_for_new_resource(sender, instance: Resource, **kwargs):
    resource = instance
    project = resource.project
    users = project.get_users()
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

    for user in users:
        if models.OfferingUser.objects.filter(
            offering=offering,
            user=user,
        ).exists():
            logger.info("An offering user for %s in %s already exists", user, offering)
            continue

        username = utils.generate_username(user, offering)

        offering_user = models.OfferingUser.objects.create(
            offering=offering,
            user=user,
            username=username,
        )

        utils.setup_linux_related_data(offering_user, offering)
        offering_user.save(update_fields=["backend_metadata"])

        logger.info("The offering user %s has been created", offering_user)


def update_offering_user_username_after_offering_settings_change(
    sender, instance: Offering, created=False, **kwargs
):
    if created:
        return

    offering = instance

    if (
        offering.type not in OFFERING_USER_ALLOWED_OFFERING_TYPES
        or not offering.tracker.has_changed("plugin_options")
    ):
        return

    offering_users = models.OfferingUser.objects.filter(offering=offering)

    for offering_user in offering_users:
        new_username = utils.generate_username(offering_user.user, offering)
        logger.info("New username for %s is %s", offering_user, new_username)
        offering_user.username = new_username

        utils.setup_linux_related_data(offering_user, offering)
        offering_user.save(update_fields=["username", "backend_metadata"])


def update_offering_user_username_after_user_change(sender, instance: User, **kwargs):
    """Set new username for offering users after site_username in user details has been changed."""
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
        new_username = utils.generate_username(user, offering)
        logger.info("Setting username for %s to %s", offering_user, new_username)
        offering_user.username = new_username

        utils.setup_linux_related_data(offering_user, offering)
        offering_user.save(update_fields=["username", "backend_metadata"])


def update_offering_user_username_after_freeipa_profile_update(
    sender, instance: Profile, created=False, **kwargs
):
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
        new_username = utils.generate_username(profile.user, offering_user.offering)

        logger.info("Setting username for %s to %s", offering_user, new_username)
        offering_user.username = new_username
        offering_user.save(update_fields=["username"])


def notify_user_about_rejected_order(sender, instance: Order, created=False, **kwargs):
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


def log_offering_role_created_or_updated(
    sender, instance: OfferingUserRole, created=False, **kwargs
):
    if created:
        event_logger.info(
            f"Offering role {instance.name} has been created.",
            event_type="marketplace_offering_role_created",
            event_context={
                "offering_role": instance,
            },
            group="marketplace_offering_role",
        )
    else:
        event_logger.info(
            f"Offering role {instance.name} has been updated.",
            event_type="marketplace_offering_role_updated",
            event_context={
                "offering_role": instance,
            },
            group="marketplace_offering_role",
        )


def log_resource_user_created(
    sender, instance: models.ResourceUser, created=False, **kwargs
):
    if created:
        event_logger.info(
            f"User {instance.user.username} has been assigned"
            f" role {instance.role.name} in resource {instance.resource.name}.",
            event_type="marketplace_resource_user_created",
            event_context={
                "resource_user": instance,
            },
            group="marketplace_resource_user",
        )


def log_offering_role_deleted(sender, instance: OfferingUserRole, **kwargs):
    event_logger.info(
        f"Offering role {instance.name} has been deleted.",
        event_type="marketplace_offering_role_deleted",
        event_context={
            "offering_role": instance,
        },
        group="marketplace_offering_role",
    )


def log_resource_user_deleted(sender, instance: models.ResourceUser, **kwargs):
    event_logger.info(
        f"User {instance.user.username} has been unassigned"
        f" role {instance.role.name} in resource {instance.resource.name}.",
        event_type="marketplace_resource_user_deleted",
        event_context={
            "resource_user": instance,
        },
        group="marketplace_resource_user",
    )
