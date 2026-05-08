import logging
from datetime import datetime

from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.template import Context, Template
from django.template.loader import get_template
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.models import SshPublicKey
from waldur_core.permissions.models import UserRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    OrderStates,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace_support import utils as marketplace_support_utils
from waldur_mastermind.support import backend as support_backend
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.models import Issue

from . import tasks

logger = logging.getLogger(__name__)


RESOURCE_CALLBACKS = {
    (OrderTypes.CREATE, True): callbacks.resource_creation_succeeded,
    (OrderTypes.CREATE, False): callbacks.resource_creation_canceled,
    (OrderTypes.UPDATE, True): callbacks.resource_update_succeeded,
    (OrderTypes.UPDATE, False): callbacks.resource_update_failed,
    (OrderTypes.TERMINATE, True): callbacks.resource_deletion_succeeded,
    (OrderTypes.TERMINATE, False): callbacks.resource_deletion_failed,
}


def _update_order_output_safely(order: marketplace_models.Order, issue: Issue):
    """
    Update order.output with issue details. Fail-safe - errors won't stop processing.
    """
    try:
        # Collect only public information
        output_data = {
            "timestamp": datetime.now().isoformat(),
            "issue_key": issue.key,
            "issue_status": issue.status,
            "resolution": "Success" if issue.resolved else "Failed/Canceled",
            "backend": issue.backend_name,
        }

        # Add timeline information
        if issue.created:
            output_data["issue_created"] = issue.created.isoformat()
        if issue.modified:
            output_data["last_updated"] = issue.modified.isoformat()
        if issue.resolution_date:
            output_data["resolution_date"] = issue.resolution_date.isoformat()

        # Just indicate if assigned (no names for privacy)
        if issue.assignee:
            output_data["is_assigned"] = True

        # Add only PUBLIC comments, without author names
        try:
            public_comments = issue.comments.filter(is_public=True).order_by(
                "-created"
            )[:3]
            if public_comments:
                output_data["recent_public_updates"] = [
                    {
                        "date": comment.created.isoformat()
                        if comment.created
                        else None,
                        "message": comment.description[:200]
                        if comment.description
                        else "",
                    }
                    for comment in public_comments
                ]
        except Exception as e:
            logger.warning(f"Failed to fetch comments for order {order.uuid}: {e}")

        # Format as human-readable text (consistent with other offering types)
        output_lines = [
            f"Issue: {output_data['issue_key']} ({output_data['backend']})",
            f"Status: {output_data['issue_status']}",
            f"Resolution: {output_data['resolution']}",
            f"Updated: {output_data['timestamp']}",
        ]

        if output_data.get("issue_created"):
            output_lines.append(f"Created: {output_data['issue_created']}")

        if output_data.get("is_assigned"):
            output_lines.append("Assigned: Yes")

        if output_data.get("recent_public_updates"):
            output_lines.append("")  # Empty line separator
            output_lines.append("Recent Updates:")
            for update in output_data["recent_public_updates"]:
                date_str = update["date"][:19] if update.get("date") else "Unknown"
                output_lines.append(f"  • {date_str}: {update['message'][:100]}...")

        order.output = "\n".join(output_lines)
        order.save(update_fields=["output"])

    except Exception as e:
        logger.error(
            f"Failed to update output for order {order.uuid}: {e}. "
            f"Continuing with main processing."
        )
        # Try minimal fallback
        try:
            order.output = f"Issue: {getattr(issue, 'key', 'unknown')}\nStatus: Output generation failed\nTime: {datetime.now().isoformat()}"
            order.save(update_fields=["output"])
        except Exception:
            logger.exception(f"Failed to set fallback output for order {order.uuid}")


def update_order_if_issue_was_complete(
    sender, instance: Issue, created=False, **kwargs
):
    issue = instance
    old_status = issue.tracker.previous("status")

    # Prevent recursion: if only processing_log or backend_name changed, skip processing
    # backend_name can be set by BackendNameMixin.save() after our processing_log save
    update_fields = kwargs.get("update_fields")
    if update_fields is not None:
        update_fields_set = set(update_fields)
        # Skip if this is just a processing_log or backend_name update (not a real status change)
        if update_fields_set and not update_fields_set - {
            "processing_log",
            "backend_name",
        }:
            return

    logger.info(
        "update_order_if_issue_was_complete triggered. "
        "Issue key=%s, status=%s (was %s), created=%s, has_resource=%s",
        issue.key,
        issue.status,
        old_status,
        created,
        bool(issue.resource),
    )

    if created:
        return

    if not issue.tracker.has_changed("status"):
        return

    # Check all conditions and log detailed info
    has_resource = bool(issue.resource)
    is_order = (
        isinstance(issue.resource, marketplace_models.Order) if has_resource else False
    )
    offering_type = issue.resource.offering.type if is_order else None
    is_support_offering = offering_type == SUPPORT_OFFERING if offering_type else False
    resolved_value = issue.resolved

    logger.info(
        "Condition check for issue %s: has_resource=%s, is_order=%s, "
        "offering_type=%s, is_support_offering=%s, resolved=%s",
        issue.key,
        has_resource,
        is_order,
        offering_type,
        is_support_offering,
        resolved_value,
    )

    # Log to processing_log for staff visibility
    # Build all log entries first, then save once at the end to avoid multiple saves
    log_entries = []

    log_entries.append(
        {
            "event": "status_changed",
            "old_status": old_status,
            "new_status": issue.status,
            "has_resource": has_resource,
            "is_order": is_order,
            "offering_type": str(offering_type) if offering_type else None,
            "is_support_offering": is_support_offering,
            "resolved_value": resolved_value,
            "resolved_type": type(resolved_value).__name__,
        }
    )

    if not (
        has_resource and is_order and is_support_offering and resolved_value is not None
    ):
        reason = []
        if not has_resource:
            reason.append("no_resource")
        if not is_order:
            reason.append("not_an_order")
        if not is_support_offering:
            reason.append(f"wrong_offering_type({offering_type})")
        if resolved_value is None:
            reason.append("resolved_is_none")

        logger.warning(
            "Skipping order processing for issue %s: conditions not met. Reasons: %s",
            issue.key,
            ", ".join(reason),
        )
        log_entries.append(
            {
                "event": "processing_skipped",
                "reasons": reason,
            }
        )

        # Save all log entries using direct update to avoid triggering signals
        for entry in log_entries:
            event = entry.pop("event")
            log_entry = {
                "timestamp": timezone.now().isoformat(),
                "event": event,
                "details": entry if entry else None,
            }
            if issue.processing_log is None:
                issue.processing_log = []
            issue.processing_log.append(log_entry)

        Issue.objects.filter(pk=issue.pk).update(processing_log=issue.processing_log)
        return

    order = issue.resource

    logger.info(
        "Processing order %s (uuid=%s, type=%s, state=%s) for issue %s with resolved=%s",
        order.id,
        order.uuid,
        order.type,
        order.state,
        issue.key,
        resolved_value,
    )

    # Update order output (fail-safe - won't stop callback)
    _update_order_output_safely(order, issue)

    callback_key = (order.type, resolved_value)
    callback = RESOURCE_CALLBACKS[callback_key]

    logger.info(
        "Invoking callback %s for order %s (key=%s)",
        callback.__name__,
        order.uuid,
        callback_key,
    )

    log_entries.append(
        {
            "event": "callback_invoked",
            "order_uuid": str(order.uuid),
            "order_type": order.type,
            "order_state_before": order.state,
            "callback": callback.__name__,
            "callback_key": str(callback_key),
        }
    )

    try:
        result = callback(order.resource)
        order.refresh_from_db()

        logger.info(
            "Callback %s completed for order %s. Order state after: %s, result: %s",
            callback.__name__,
            order.uuid,
            order.state,
            result,
        )

        log_entries.append(
            {
                "event": "callback_completed",
                "order_state_after": order.state,
                "result_order_uuid": str(result.uuid) if result else None,
            }
        )
    except Exception as e:
        logger.exception(
            "Callback %s failed for order %s: %s",
            callback.__name__,
            order.uuid,
            str(e),
        )
        log_entries.append(
            {
                "event": "callback_failed",
                "error": str(e),
            }
        )

    # Save all log entries at once using direct update to avoid triggering signals
    for entry in log_entries:
        event = entry.pop("event")
        log_entry = {
            "timestamp": timezone.now().isoformat(),
            "event": event,
            "details": entry if entry else None,
        }
        if issue.processing_log is None:
            issue.processing_log = []
        issue.processing_log.append(log_entry)

    # Use update() to avoid triggering post_save signal
    Issue.objects.filter(pk=issue.pk).update(processing_log=issue.processing_log)


def notify_about_request_based_item_creation(
    sender, instance: Issue, created=False, **kwargs
):
    if created:
        return

    issue = instance

    if not issue.tracker.has_changed("backend_id"):
        return

    if not (
        issue.resource
        and isinstance(issue.resource, marketplace_models.Order)
        and issue.resource.offering.type == SUPPORT_OFFERING
        and issue.resource.type == OrderTypes.CREATE
    ):
        return

    order = issue.resource
    service_provider = getattr(order.offering.customer, "serviceprovider", None)

    if not service_provider:
        logger.warning(
            "Customer providing an Offering is not registered as a Service Provider."
        )
        return

    if not service_provider.lead_email:
        return

    attributes_with_display_names = {}

    for attribute_key, attribute_value in order.attributes.items():
        if attribute_key in order.offering.options["options"].keys():
            display_name = order.offering.options["options"][attribute_key]["label"]
            attributes_with_display_names[display_name] = attribute_value
            continue

        attributes_with_display_names[attribute_key] = attribute_value

    setattr(order, "attributes_with_display_names", attributes_with_display_names)

    context = Context({"order": order, "issue": issue}, autoescape=False)
    template = Template(service_provider.lead_body)
    message = template.render(context).strip()
    template = Template(service_provider.lead_subject)
    subject = template.render(context).strip()

    transaction.on_commit(
        lambda: tasks.send_mail_notification.delay(
            subject, message, service_provider.lead_email
        )
    )


def _create_issue_for_project_membership_changed(instance, summary):
    user_role = instance
    project = user_role.scope
    logger.info(
        "Checking resources for project %s (id: %s) with PLUGIN_NAME %s",
        project.name,
        project.id,
        SUPPORT_OFFERING,
    )

    resources = marketplace_models.Resource.objects.exclude(
        state=ResourceStates.TERMINATED
    ).filter(
        project=project,
        offering__type=SUPPORT_OFFERING,
        offering__plugin_options__enable_issues_for_membership_changes=True,
    )

    if resources.exists():
        logger.info(
            "Found %d active resources with enabled membership change notifications",
            resources.count(),
        )
        offering_ids = resources.values_list("offering_id", flat=True).distinct()
        offerings = marketplace_models.Offering.objects.filter(id__in=offering_ids)

        for offering in offerings:
            logger.debug("Processing offering %s (id: %s)", offering.name, offering.id)
            resources = marketplace_models.Resource.objects.filter(
                offering=offering, project=project
            )
            setattr(offering, "resources", resources)
            offering_user = offering.offeringuser_set.filter(
                user=user_role.user
            ).first()
            setattr(offering, "offering_user", offering_user)

        template = get_template(
            "marketplace_support/create_project_membership_update_issue.txt"
        ).template
        description = template.render(
            Context(
                {
                    "offerings": offerings,
                    "project": project,
                    "user": user_role.user,
                    "role": user_role.role.name,
                    "project_url": core_utils.format_homeport_link(
                        "projects/{project_uuid}/", project_uuid=project.uuid.hex
                    ),
                },
                autoescape=False,
            )
        )
        try:
            logger.info(
                "Creating issue for membership change. User: %s, Project: %s, Organization: %s",
                user_role.user.full_name,
                project.name,
                project.customer.get_display_name(),
            )
            marketplace_support_utils.create_issue_about_project_team_changes(
                project,
                created_by=user_role.user,
                summary=summary.format(
                    user=user_role.user.full_name,
                    project=project.name,
                    role=user_role.role.name,
                    organization=project.customer.get_display_name(),
                ),
                description=description,
            )
        except Exception as e:
            logger.exception(
                "Failed to create issue for membership change. User: %s, Project: %s. Error: %s",
                user_role.user.full_name,
                project.name,
                str(e),
            )
            raise
    else:
        logger.debug("No eligible resources found for membership change notification")


def _create_issue_for_resource_membership_changed(
    instance, summary, *, resource, resource_project=None
):
    user_role = instance
    if resource.state == ResourceStates.TERMINATED:
        logger.debug("Skipping - resource is terminated")
        return

    offering = resource.offering
    if offering.type != SUPPORT_OFFERING:
        logger.debug("Skipping - resource offering is not a support offering")
        return

    if not offering.plugin_options.get("enable_issues_for_membership_changes"):
        logger.debug("Skipping - enable_issues_for_membership_changes is not set")
        return

    project = resource.project
    offering_user = offering.offeringuser_set.filter(user=user_role.user).first()
    resource_url = core_utils.format_homeport_link(
        "resource-details/{resource_uuid}/",
        project_uuid=project.uuid.hex,
        resource_uuid=resource.uuid.hex,
    )
    project_url = core_utils.format_homeport_link(
        "projects/{project_uuid}/", project_uuid=project.uuid.hex
    )

    template = get_template(
        "marketplace_support/create_resource_membership_update_issue.txt"
    ).template
    description = template.render(
        Context(
            {
                "resource": resource,
                "resource_project": resource_project,
                "project": project,
                "user": user_role.user,
                "role": user_role.role.name,
                "offering": offering,
                "offering_user": offering_user,
                "resource_url": resource_url,
                "project_url": project_url,
            },
            autoescape=False,
        )
    )

    try:
        logger.info(
            "Creating issue for resource membership change. User: %s, Resource: %s, Organization: %s",
            user_role.user.full_name,
            resource.name,
            project.customer.get_display_name(),
        )
        marketplace_support_utils.create_issue_about_project_team_changes(
            project,
            created_by=user_role.user,
            summary=summary.format(
                user=user_role.user.full_name,
                resource=resource.name,
                project=project.name,
                role=user_role.role.name,
                organization=project.customer.get_display_name(),
            ),
            description=description,
        )
    except Exception as e:
        logger.exception(
            "Failed to create issue for resource membership change. User: %s, Resource: %s. Error: %s",
            user_role.user.full_name,
            resource.name,
            str(e),
        )
        raise


PROJECT_MEMBERSHIP_SUMMARIES = {
    True: "{organization}: User {user} has been added to project '{project}' with role '{role}'.",
    False: "{organization}: User {user} has been removed from project '{project}' with role '{role}'.",
}

RESOURCE_MEMBERSHIP_SUMMARIES = {
    True: "{organization}: User {user} has been added to resource '{resource}' with role '{role}'.",
    False: "{organization}: User {user} has been removed from resource '{resource}' with role '{role}'.",
}

RESOURCE_PROJECT_MEMBERSHIP_SUMMARIES = {
    True: "{organization}: User {user} has been added to resource '{resource}' (project '{project}') with role '{role}'.",
    False: "{organization}: User {user} has been removed from resource '{resource}' (project '{project}') with role '{role}'.",
}


def create_issue_if_membership_changed(
    sender, instance: UserRole, created=False, **kwargs
):
    """Create support issue when user role membership changes in organization."""
    logger.info(
        "Handling membership change event. Created: %s, Instance: %s, Is active: %s",
        created,
        instance,
        instance.is_active if hasattr(instance, "is_active") else "N/A",
    )

    if created and not instance.is_active:
        logger.debug("Skipping - newly created inactive instance")
        return

    if not instance.tracker.has_changed("is_active"):
        logger.debug("Skipping - is_active status hasn't changed")
        return

    scope = instance.scope
    is_active = bool(instance.is_active)

    if isinstance(scope, structure_models.Project):
        _create_issue_for_project_membership_changed(
            instance, PROJECT_MEMBERSHIP_SUMMARIES[is_active]
        )
    elif isinstance(scope, marketplace_models.Resource):
        _create_issue_for_resource_membership_changed(
            instance,
            RESOURCE_MEMBERSHIP_SUMMARIES[is_active],
            resource=scope,
        )
    elif isinstance(scope, marketplace_models.ResourceProject):
        _create_issue_for_resource_membership_changed(
            instance,
            RESOURCE_PROJECT_MEMBERSHIP_SUMMARIES[is_active],
            resource=scope.resource,
            resource_project=scope,
        )
    else:
        logger.debug("Skipping - unsupported scope type %s", type(scope).__name__)
        return


def _create_issue_for_ssh_key_change(ssh_key, summary):
    user = ssh_key.user
    active_backend = support_backend.get_active_backend()
    issue_details = active_backend.get_issue_details()

    project_ct = ContentType.objects.get_for_model(structure_models.Project)
    user_project_ids = UserRole.objects.filter(
        user=user,
        is_active=True,
        content_type=project_ct,
    ).values_list("object_id", flat=True)

    resources = (
        marketplace_models.Resource.objects.exclude(state=ResourceStates.TERMINATED)
        .filter(
            offering__type=SUPPORT_OFFERING,
            project_id__in=user_project_ids,
        )
        .select_related(
            "project",
            "project__customer",
            "offering",
        )
    )

    template = get_template("marketplace_support/ssh_key_change_issue.txt").template
    description = template.render(
        Context(
            {
                "user": user,
                "ssh_key": ssh_key,
                "resources": resources,
            },
            autoescape=False,
        )
    )

    if active_backend.message_format == support_backend.SupportedFormat.HTML:
        description = core_utils.text2html(description)

    issue_details.update(
        dict(
            caller=user,
            description=description,
            summary=summary,
        )
    )

    issue = support_models.Issue.objects.create(**issue_details)
    active_backend.create_issue(issue)
    issue.refresh_from_db()
    return issue


def create_issue_for_pending_support_order(sender, instance, created=False, **kwargs):
    """
    Create a support ticket in the background when a support offering order
    enters PENDING_START_DATE or PENDING_PROJECT state, so providers
    see the ticket right away even though provisioning is deferred.
    """
    order = instance
    if created:
        return

    if order.offering.type != SUPPORT_OFFERING:
        return

    pending_states = (
        OrderStates.PENDING_START_DATE,
        OrderStates.PENDING_PROJECT,
    )
    if order.state not in pending_states:
        return

    serialized_order = core_utils.serialize_instance(order)
    transaction.on_commit(
        lambda: tasks.create_issue_for_pending_order.delay(serialized_order)
    )


def create_issue_if_ssh_key_added(
    sender, instance: SshPublicKey, created=False, **kwargs
):
    if not created:
        return

    if not config.WALDUR_SUPPORT_ENABLED:
        return

    if not config.ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES:
        return

    _create_issue_for_ssh_key_change(
        instance,
        f"SSH key {instance.name} has been added by user {instance.user.full_name or instance.user.username}.",
    )


def create_issue_if_ssh_key_removed(sender, instance: SshPublicKey, **kwargs):
    if not config.WALDUR_SUPPORT_ENABLED:
        return

    if not config.ENABLE_ISSUES_FOR_USER_SSH_KEY_CHANGES:
        return

    _create_issue_for_ssh_key_change(
        instance,
        f"SSH key {instance.name} has been removed by user {instance.user.full_name or instance.user.username}.",
    )
