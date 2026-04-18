import logging

from django.contrib.contenttypes.models import ContentType
from django.template import Context
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions

from waldur_core.core.utils import format_homeport_link, text2html
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.utils import format_limits_list, get_order_url
from waldur_mastermind.support import backend as support_backend
from waldur_mastermind.support import exceptions as support_exceptions
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support import serializers as support_serializers

logger = logging.getLogger(__name__)


def get_order_issue(order):
    order_content_type = ContentType.objects.get_for_model(order)
    return support_models.Issue.objects.get(
        resource_object_id=order.id,
        resource_content_type=order_content_type,
    )


def get_request_link(resource: marketplace_models.Resource):
    return format_homeport_link(
        "projects/{project_uuid}/support/{request_uuid}/",
        project_uuid=resource.project.uuid,
        request_uuid=resource.uuid,
    )


def format_description(template_name, context):
    template = get_template("marketplace_support/" + template_name + ".txt")
    return template.template.render(Context(context, autoescape=False))


def format_create_description(order):
    result = []

    for key in order.offering.options.get("order") or []:
        if key not in order.attributes:
            continue

        label = order.offering.options["options"].get(key, {})
        label_value = label.get("label", key)
        result.append(f"{label_value}: '{order.attributes[key]}'")

    if "description" in order.attributes:
        result.append("\n %s" % order.attributes["description"])

    result.append(
        format_description(
            "create_resource_template",
            {
                "order": order,
                "order_url": get_order_url(order),
                "resource": order.resource,
            },
        )
    )

    if order.limits:
        components_map = order.offering.get_limit_components()
        for key, value in order.limits.items():
            component = components_map.get(key)
            if component:
                result.append(
                    f"\n{component.name} ({component.type}): {value} {component.measured_unit}"
                )

    # Add total cost (includes prepaid duration multiplier via order.init_cost)
    if order.cost is not None:
        result.append(f"\nTotal cost: {order.cost:.2f}")

    # Add resource, project and customer slugs
    resource = order.resource
    if resource:
        result.append(f"\nResource slug: {resource.slug}")
    if order.project:
        result.append(f"Project slug: {order.project.slug}")
        if order.project.customer:
            result.append(f"Customer slug: {order.project.customer.slug}")

    description = "\n".join(result)

    return description


def create_issue(order, description, summary, confirmation_comment=None):
    order_content_type = ContentType.objects.get_for_model(order)
    active_backend = support_backend.get_active_backend()

    if support_models.Issue.objects.filter(
        resource_object_id=order.id, resource_content_type=order_content_type
    ).exists():
        logger.warning(
            "An issue creating is skipped because an issue for order %s exists already.",
            order.uuid,
        )
        return

    issue_details = active_backend.get_issue_details()

    issue_details.update(
        dict(
            caller=order.created_by,
            project=order.project,
            customer=order.project.customer,
            description=description,
            summary=summary,
            resource=order,
        )
    )
    issue_details["summary"] = support_serializers.render_issue_template(
        "ATLASSIAN_SUMMARY_TEMPLATE", "summary", issue_details
    )
    issue_details["description"] = support_serializers.render_issue_template(
        "ATLASSIAN_DESCRIPTION_TEMPLATE", "description", issue_details
    )

    if (
        support_backend.get_active_backend().message_format
        == support_backend.SupportedFormat.HTML
    ):
        issue_details["description"] = text2html(issue_details["description"])

    issue = support_models.Issue.objects.create(**issue_details)
    try:
        active_backend.create_issue(issue)
        issue.refresh_from_db()
    except support_exceptions.SupportUserInactive:
        issue.delete()
        order.resource.set_state_erred()
        order.resource.save(update_fields=["state"])
        raise rf_exceptions.ValidationError(
            _(
                "Delete resource process is cancelled and issue not created "
                "because a caller is inactive."
            )
        )
    except ServiceBackendError as e:
        issue.delete()
        order.resource.set_state_erred()
        order.resource.save(update_fields=["state"])
        raise rf_exceptions.ValidationError(e)

    ids = marketplace_models.Order.objects.filter(resource=order.resource).values_list(
        "id", flat=True
    )
    linked_issues = support_models.Issue.objects.filter(
        resource_object_id__in=ids,
        resource_content_type=order_content_type,
    ).exclude(id=issue.id)
    try:
        active_backend.create_issue_links(issue, list(linked_issues))
    except ServiceBackendError as e:
        logger.exception("Linked issues have not been added: %s", e)

    if confirmation_comment:
        try:
            active_backend.create_confirmation_comment(issue, confirmation_comment)
        except ServiceBackendError as e:
            logger.exception("Unable to create confirmation comment: %s", e)

    if order.attachment:
        try:
            attachment = support_models.Attachment.objects.create(
                issue=issue,
                file=order.attachment,
            )
            active_backend.create_attachment(attachment)
        except Exception as e:
            logger.exception(
                "Unable to attach purchase order for order %s: %s", order.uuid, e
            )

    return issue


def format_update_description(order):
    request_url = get_request_link(order.resource)
    return format_description(
        "update_resource_template",
        {"order": order, "request_url": request_url},
    )


def format_update_limits_description(order):
    offering = order.resource.offering
    request_url = get_request_link(order.resource)
    components_map = offering.get_limit_components()
    old_limits = format_limits_list(components_map, order.resource.limits)
    new_limits = format_limits_list(components_map, order.limits)
    context = {
        "order": order,
        "request_url": request_url,
        "old_limits": old_limits,
        "new_limits": new_limits,
    }
    return format_description(
        "update_limits_template",
        context,
    )


def format_delete_description(order):
    request_url = get_request_link(order.resource)
    return format_description(
        "terminate_resource_template",
        {"order": order, "request_url": request_url},
    )


def create_issue_about_project_team_changes(project, created_by, summary, description):
    logger.info(
        "Creating issue about project team changes. Project: %s, Created by: %s",
        project.name,
        created_by.username,
    )

    active_backend = support_backend.get_active_backend()
    logger.debug("Using support backend: %s", active_backend.backend_name)

    issue_details = active_backend.get_issue_details()
    logger.debug("Got issue details from backend: %s", issue_details)

    issue_details.update(
        dict(
            caller=created_by,
            project=project,
            customer=project.customer,
            description=description,
            summary=summary,
        )
    )

    if (
        support_backend.get_active_backend().message_format
        == support_backend.SupportedFormat.HTML
    ):
        issue_details["description"] = text2html(issue_details["description"])
        logger.debug("Converted description to HTML format")

    try:
        logger.info(
            "Creating issue with details: %s",
            {k: v for k, v in issue_details.items() if k != "description"},
        )
        issue = support_models.Issue.objects.create(**issue_details)
        logger.info("Created issue object in database with ID: %s", issue.id)

        try:
            active_backend.create_issue(issue)
            logger.info("Successfully created issue in backend system")
            issue.refresh_from_db()
        except support_exceptions.SupportUserInactive:
            logger.error(
                "Failed to create issue - caller %s is inactive in support system",
                created_by.username,
            )
            issue.delete()
            raise rf_exceptions.ValidationError(
                _(
                    "Delete resource process is cancelled and issue not created "
                    "because a caller is inactive."
                )
            )
        except ServiceBackendError as e:
            logger.error(
                "Service backend error while creating issue: %s. User: %s, Project: %s",
                str(e),
                created_by.username,
                project.name,
            )
            issue.delete()
            raise rf_exceptions.ValidationError(e)
    except Exception as e:
        logger.exception(
            "Unexpected error while creating issue. User: %s, Project: %s. Error: %s",
            created_by.username,
            project.name,
            str(e),
        )
        raise

    return issue
