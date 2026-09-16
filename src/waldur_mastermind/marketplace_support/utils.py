import logging

from django.contrib.contenttypes.models import ContentType
from django.template import Context
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions as rf_exceptions

from waldur_core.core.utils import format_homeport_link, text2html
from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.utils import get_users
from waldur_core.structure.exceptions import ServiceBackendError
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OrderTypes
from waldur_mastermind.marketplace.utils import format_limits_list, get_order_url
from waldur_mastermind.proposal import models as proposal_models
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

    if order.type == OrderTypes.RESTORE:
        result.append(
            "This is a restoration request for a previously terminated resource."
        )

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
        components_map = order.offering.get_limit_components(order.plan)
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


# Roles to fall back through, most accountable first, when the order was placed
# by something other than a reachable person.
CALLER_FALLBACK_PROJECT_ROLES = (
    RoleEnum.PROJECT_MANAGER,
    RoleEnum.PROJECT_ADMIN,
    RoleEnum.PROJECT_MEMBER,
)


def _first_reachable_user(scope, role_name):
    """First user holding ``role_name`` on ``scope`` who has an email.

    Ordered by id so the pick is stable: a project with several managers must
    not hand consecutive tickets to different people. ``get_users`` queries
    through ``User.objects``, which already excludes deactivated users -- an
    inactive caller is refused by the backend anyway.
    """
    return get_users(scope, role_name).exclude(email="").order_by("id").first()


def _reachable(user):
    """The user, if they can actually receive a ticket, else None.

    Unlike ``_first_reachable_user`` this checks ``is_active`` itself: the
    configured callers are plain foreign keys, not queried through the active
    manager.
    """
    if user and user.is_active and user.email:
        return user
    return None


def _configured_caller(order):
    """The person the order's call wants its tickets raised on behalf of.

    Reached through ``RequestedResource``, which allocation points at the
    resource it created. Going via the project instead would be ambiguous: a
    project can carry more than one proposal, and the lowest-numbered one is
    not necessarily the one that produced this resource -- or even an accepted
    one.

    None when the order did not come from a proposal, or when the configured
    person cannot receive a ticket -- the role chain then decides, rather than
    failing an order somebody on the project could have answered for.
    """
    if not order.resource_id:
        return None

    requested = (
        proposal_models.RequestedResource.objects.filter(resource=order.resource)
        .select_related(
            "proposal__created_by",
            "proposal__round__call__support_ticket_caller_user",
        )
        .first()
    )
    if requested is None:
        return None

    call = requested.proposal.round.call
    choice = call.support_ticket_caller

    if choice == call.TicketCaller.APPLICANT:
        caller = _reachable(requested.proposal.created_by)
    elif choice == call.TicketCaller.SPECIFIC_USER:
        caller = _reachable(call.support_ticket_caller_user)
    elif choice == call.TicketCaller.PROJECT_MANAGER:
        caller = _first_reachable_user(order.project, RoleEnum.PROJECT_MANAGER)
    elif choice == call.TicketCaller.CALL_MANAGER:
        caller = _first_reachable_user(call, RoleEnum.CALL_MANAGER)
    else:
        caller = None

    if caller is None:
        # A call configured to route its tickets somewhere specific and then
        # quietly not doing so is worth a line: shared mailboxes get closed,
        # and the fallback below is invisible from the call's settings.
        logger.warning(
            "Call %s routes support tickets to '%s', but nobody reachable was "
            "found. Falling back to the roles held on project %s.",
            call.uuid.hex,
            choice,
            order.project,
        )
    return caller


def resolve_issue_caller(order):
    """Return the person the helpdesk request is raised on behalf of.

    The backend opens the request on behalf of the caller's email address, and
    Waldur addresses its own ticket notifications to them, so it has to be a
    real, reachable person.

    It does not decide who can read the ticket in the customer portal. These
    tickets carry a project and a customer, and
    ``IssueCallerOrRoleFilterBackend`` only lets a caller through on a ticket
    that has neither -- so a caller holding no role on the project gets the
    mail but cannot open the link in it.

    ``order.created_by`` is that person whenever someone placed the order
    themselves. Automated flows place orders as a robot with no email --
    proposal allocation, the scheduled end-date and cost-policy termination
    sweeps, openportal's default resources -- and an SSO user provisioned
    without an email claim is just as unusable. Both fall back to someone on
    the project who can actually receive the ticket.

    A call configures who stands in for its own allocated resources; anything
    else falls through the project's roles.

    ``created_by`` is deliberately left alone: it records who placed the order,
    and the marketplace approval gate reads its ``is_staff``.

    Returns None when nobody reachable can be found, so the caller can fail the
    order the same way the other backend failures in ``create_issue`` do.
    """
    # Same reachability test as everyone else: an order placed by someone who
    # has since been deactivated is refused by the backend with
    # SupportUserInactive, which is the failure this resolver exists to avoid.
    caller = _reachable(order.created_by)
    if caller:
        return caller

    caller = _configured_caller(order)
    if caller:
        return caller

    project = order.project
    for role_name in CALLER_FALLBACK_PROJECT_ROLES:
        caller = _first_reachable_user(project, role_name)
        if caller:
            return caller

    return _first_reachable_user(project.customer, RoleEnum.CUSTOMER_OWNER)


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

    caller = resolve_issue_caller(order)
    if caller is None:
        # Mirrors the two ServiceBackendError branches below: the resource has
        # to be marked before raising, or create_issue_for_pending_order --
        # which has no exception handling of its own -- leaves it in CREATING
        # for good.
        order.resource.set_state_erred()
        order.resource.save(update_fields=["state"])
        raise rf_exceptions.ValidationError(
            _(
                "Issue is not created because no user with an email address "
                "could be found on project %(project)s to raise it on behalf of."
            )
            % {"project": order.project.name}
        )

    issue_details.update(
        dict(
            caller=caller,
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
    components_map = offering.get_limit_components(order.resource.plan)
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


def format_renewal_description(order):
    offering = order.resource.offering
    request_url = get_request_link(order.resource)
    components_map = offering.get_limit_components(order.resource.plan)
    old_limits = format_limits_list(
        components_map, order.attributes.get("old_limits", {})
    )
    new_limits = format_limits_list(components_map, order.limits)
    resource = order.resource
    context = {
        "order": order,
        "request_url": request_url,
        "old_limits": old_limits,
        "new_limits": new_limits,
        "extension_months": order.attributes.get("extension_months", "N/A"),
        "old_end_date": order.attributes.get("old_end_date", "N/A"),
        "new_end_date": order.attributes.get("new_end_date", "N/A"),
        "cost": f"{order.cost:.2f}" if order.cost is not None else "N/A",
        "request_comment": order.request_comment or "",
        "resource_slug": getattr(resource, "slug", ""),
        "project_slug": getattr(order.project, "slug", "") if order.project else "",
        "customer_slug": getattr(order.project.customer, "slug", "")
        if order.project and order.project.customer
        else "",
    }
    return format_description("renewal_template", context)


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
