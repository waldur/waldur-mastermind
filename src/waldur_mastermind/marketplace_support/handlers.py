import logging
from datetime import datetime

from django.db import transaction
from django.template import Context, Template
from django.template.loader import get_template

from waldur_core.core import utils as core_utils
from waldur_core.permissions.models import UserRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import callbacks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    SUPPORT_OFFERING,
    OrderTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace_support import utils as marketplace_support_utils
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
    if created:
        return

    issue = instance

    if not issue.tracker.has_changed("status"):
        return

    if not (
        issue.resource
        and isinstance(issue.resource, marketplace_models.Order)
        and issue.resource.offering.type == SUPPORT_OFFERING
        and issue.resolved is not None
    ):
        return

    order = issue.resource

    # Update order output (fail-safe - won't stop callback)
    _update_order_output_safely(order, issue)

    callback = RESOURCE_CALLBACKS[(order.type, issue.resolved)]
    callback(order.resource)


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


def _create_issue_if_membership_changed(instance, summary):
    user_role = instance
    logger.info(
        "Processing membership change for user %s (id: %s) in scope %s",
        user_role.user.username,
        user_role.user.id,
        user_role.scope,
    )

    if not isinstance(user_role.scope, structure_models.Project):
        logger.debug("Skipping membership change processing - scope is not a project")
        return

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

    if not isinstance(instance.scope, structure_models.Project):
        logger.debug("Skipping - scope is not a project")
        return

    if not instance.tracker.has_changed("is_active"):
        logger.debug("Skipping - is_active status hasn't changed")
        return

    logger.info(
        "Processing membership change. User: %s, Project: %s, New status: %s",
        instance.user.username if hasattr(instance, "user") else "unknown",
        instance.scope,
        "active" if instance.is_active else "inactive",
    )

    if instance.is_active:
        _create_issue_if_membership_changed(
            instance, "{organization}: User {user} has been added to project {project}."
        )
    else:
        _create_issue_if_membership_changed(
            instance,
            "{organization}: User {user} has been removed from project {project}.",
        )
