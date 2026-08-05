import calendar
import datetime
import decimal
import json
import logging
import re

from constance import config
from django.utils import timezone

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import add_user as grant_role
from waldur_core.permissions.utils import get_permissions, validate_role_grant
from waldur_core.structure import models as structure_models
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
    get_project_users,
)
from waldur_core.users import models as user_models
from waldur_core.users import tasks as user_tasks
from waldur_core.users.enums import InvitationState
from waldur_core.users.utils import get_invitation_duplicates
from waldur_mastermind.invoices import models as invoice_models

from . import exceptions, models, utils

logger = logging.getLogger(__name__)

MAPPING = {
    "node_usage": "op_node_usage",
}

FIELD_NAMES = MAPPING.keys()

QUOTA_NAMES = MAPPING.values()


def check_managed_project_membership_control(scope, change_type):
    """
    If scope is a Project linked to a ManagedProject, check whether the
    requested change is permitted by the ManagedProject's AwardDetails.

    change_type: "membership" — add/delete members (checks can_change_membership())
                 "roles"      — update a member's role (checks can_change_roles())

    Raises PermissionDenied if the AwardDetails blocks the change.
    Does nothing if scope is not a Project or has no associated ManagedProject.
    """
    from rest_framework.exceptions import PermissionDenied

    if not isinstance(scope, structure_models.Project):
        return

    managed = models.ManagedProject.objects.filter(project=scope).first()
    if managed is None:
        return

    details = managed.get_details()
    if change_type == "membership" and not details.can_change_membership():
        raise PermissionDenied(
            "Membership changes are not allowed for this managed project."
        )
    elif change_type == "roles" and not details.can_change_roles():
        raise PermissionDenied("Role changes are not allowed for this managed project.")


def is_email_allowed_for_project(scope, email: str) -> bool:
    """
    Return whether a user with the given email address is permitted to join
    scope based on the AwardDetails email/domain restrictions.

    If scope is a Project linked to a ManagedProject, the ManagedProject's
    AwardDetails (via get_details().is_email_allowed) is authoritative and
    RemoteProjects are not consulted.

    If scope is a Project linked to one or more RemoteProjects (and no
    ManagedProject), returns True if ANY of their last_sent AwardDetails
    allows the email.  A RemoteProject with no AwardDetails yet is treated
    as unrestricted.

    Returns True for any scope that is not a Project, or a Project with no
    linked awards.
    """
    logging.debug(f"Checking if email {email} is allowed for project {scope}")

    if not isinstance(scope, structure_models.Project):
        return True

    managed = models.ManagedProject.objects.filter(project=scope).first()
    if managed is not None:
        try:
            return managed.get_details().is_email_allowed(email)
        except Exception as e:
            logger.warning(
                f"is_email_allowed_for_project: ManagedProject check failed "
                f"for {scope}: {e}"
            )
            return True

    remote_projects = list(models.RemoteProject.objects.filter(current_project=scope))
    if not remote_projects:
        return True

    for rp in remote_projects:
        try:
            details = rp.get_last_sent_details()
            if details is None or details.is_email_allowed(email):
                logging.debug(
                    f"Email {email} is allowed for project {scope} based on RemoteProject {rp.pk}"
                )
                return True
        except Exception as e:
            logger.warning(
                f"is_email_allowed_for_project: RemoteProject {rp.pk} check "
                f"failed for {scope}: {e}"
            )

    logging.debug(
        f"Email {email} is NOT allowed for project {scope} based on any RemoteProject"
    )

    return False


def assert_email_allowed_for_project(scope, email: str) -> None:
    """
    Raise PermissionDenied if the given email address is not permitted to join
    scope based on the AwardDetails domain restrictions.

    Only call this when the project.enforce_allowed_domains feature flag is
    enabled — this function does not check the flag itself.
    """
    from rest_framework.exceptions import PermissionDenied

    if not is_email_allowed_for_project(scope, email):
        raise PermissionDenied(
            "Users with this email domain are not permitted to join this project."
        )


def get_openportal_robot():
    """
    Return the OpenPortal robot user.
    This is used for system-level operations that require a user context.
    """
    from waldur_core.core import models

    robot_user, created = models.User.objects.get_or_create(
        username="openportal_robot",
        defaults={
            "is_staff": True,
            "is_active": True,
            "description": (
                "Special user used for performing actions on behalf of OpenPortal."
            ),
            "first_name": "OpenPortal",
            "last_name": "Robot",
            "email": config.SITE_EMAIL,
        },
    )
    if created:
        robot_user.set_unusable_password()
        robot_user.save(update_fields=["password"])
    return robot_user


def format_current_month():
    today = timezone.now()
    month_start = core_utils.month_start(today).strftime("%Y-%m-%d")
    month_end = core_utils.month_end(today).strftime("%Y-%m-%d")
    return month_start, month_end


def sanitize_allocation_name(name):
    incorrect_symbols_regex = r"[^%s]+" % models.OPENPORTAL_ALLOCATION_REGEX
    return re.sub(incorrect_symbols_regex, "", name)


def get_customer_allocations(user):
    """
    Return the allocations to the user associated with being a customer.
    This will typically be all of the allocations associated with customer
    roles in, e.g. an organisation
    """
    connected_customers = get_connected_customers(user)

    customer_allocations = models.Allocation.objects.filter(
        is_active=True, project__customer__in=connected_customers
    )

    return customer_allocations


def get_project_allocations(user):
    """
    Return all of the allocations associated with the passed user
    to any project. This gives the projects in which the user is active.
    Projects in which the user is inactive are ignored
    """
    connected_projects = get_connected_projects(user)

    project_allocations = models.Allocation.objects.filter(
        is_active=True, project__in=connected_projects
    )

    return project_allocations


def get_remote_project_allocations(user):
    """
    Return all of the remote allocations associated with the passed user
    to any project. This gives the projects in which the user is active.
    Projects in which the user is inactive are ignored
    """
    connected_projects = get_connected_projects(user)

    project_allocations = models.RemoteAllocation.objects.filter(
        is_active=True, project__in=connected_projects
    )

    return project_allocations


def set_default_project_shortname(project):
    """
    Set and return the default shortname for the passed project.
    If the project already has a shortname, the original
    shortname will be returned
    """
    # Use the project slug as the default shortname
    if project.slug is None:
        logger.error(f"Project slug is None for project: {project}")
        raise ValueError(f"Project slug is None for project: {project}")

    shortname = str(project.slug).strip()

    if len(shortname) == 0:
        logger.error(f"Project slug is empty for project: {project}")
        raise ValueError(f"Project slug is empty for project: {project}")

    if len(shortname) > models.MAX_PROJECT_SHORTNAME_LENGTH:
        logger.warning(
            f"Project slug '{shortname}' is longer than {models.MAX_PROJECT_SHORTNAME_LENGTH} characters for project: {project}"
        )
        shortname = shortname[: models.MAX_PROJECT_SHORTNAME_LENGTH]

    project_info, created = models.ProjectInfo.objects.get_or_create(
        project=project, shortname=shortname
    )

    if created:
        project_info.sanitise()
    else:
        logger.warning(
            f"ProjectInfo already exists for project {project} with shortname {project_info.shortname}"
        )

    if project_info.shortname is None:
        logger.error(f"Empty shortname for project: {project}")
        raise ValueError(f"Empty shortname for project: {project}")

    return project_info.shortname


def get_project_shortname(project):
    """
    Return the preferred shortname for the passed project.
    """
    # look up the short name from the models.ProjectInfo object
    # associated with this project
    project_info, created = models.ProjectInfo.objects.get_or_create(project=project)

    project_info.sanitise()

    if project_info.shortname is None:
        logger.error(f"Empty shortname for project: {project}")

    return project_info.shortname


def get_user_shortname(user):
    """
    Return the preferred shortname for the passed user.
    """
    # look up the short name from the models.UserInfo object
    # associated with this user
    user_info, created = models.UserInfo.objects.get_or_create(user=user)

    user = user_info.user

    # if this is not set, then copy it in from the user.unix_username
    # property (which may disappear in the future)
    if user_info.shortname is None and hasattr(user, "unix_username"):
        if user.unix_username is not None:
            logger.debug(f"Copying shortname from the user's unix_username for {user}")
            user_info.set_shortname(user.unix_username)
            user_info.save()

    if user_info.shortname is None:
        logger.error(f"Empty shortname for user: {user}")

    return user_info.shortname


def get_first_day_of_month(date):
    """
    Return the first day of the month for the given date.
    """
    return date.replace(day=1)


def get_last_day_of_month(date):
    """
    Return the last day of the month for the given date.
    """
    next_month = date.replace(day=28) + timezone.timedelta(days=4)
    return next_month - timezone.timedelta(days=next_month.day)


def get_association(user, allocation):
    """
    Return the association between the user and the allocation.
    """
    if not isinstance(allocation, models.Allocation):
        raise TypeError("allocation must be an instance of models.Allocation")

    if not isinstance(user, core_models.User):
        raise TypeError("user must be an instance of core_models.User")

    try:
        return models.Association.objects.get(user=user, allocation=allocation)
    except models.Association.MultipleObjectsReturned:
        logger.warning(
            f"Multiple associations found for {user} and {allocation} - removing all but the first one"
        )
        associations = models.Association.objects.filter(
            user=user, allocation=allocation
        )

        if associations.exists():
            first_association = associations.first()

            if first_association is None:
                logger.error(f"No associations found for {user} and {allocation}?")
                raise models.Association.DoesNotExist(
                    f"No association found for {user} and {allocation}"
                )

            if len(associations) > 1:
                for association in associations[1:]:
                    logger.info(
                        f"Deleting duplicate association {association} for {user} and {allocation}"
                    )
                    association.delete()

            return first_association
        else:
            logger.error(
                f"No associations found for {user} and {allocation} after deletion"
            )
            raise models.Association.DoesNotExist(
                f"No association found for {user} and {allocation}"
            )


def get_remote_association(user, allocation):
    """
    Return the association between the user and the allocation.
    """
    if not isinstance(allocation, models.RemoteAllocation):
        raise TypeError("allocation must be an instance of models.RemoteAllocation")

    if not isinstance(user, core_models.User):
        raise TypeError("user must be an instance of core_models.User")

    try:
        return models.RemoteAssociation.objects.get(user=user, allocation=allocation)
    except models.RemoteAssociation.MultipleObjectsReturned:
        logger.warning(
            f"Multiple associations found for {user} and {allocation} - removing all but the first one"
        )
        associations = models.RemoteAssociation.objects.filter(
            user=user, allocation=allocation
        )

        if associations.exists():
            first_association = associations.first()

            if first_association is None:
                logger.error(f"No associations found for {user} and {allocation}?")
                raise models.RemoteAssociation.DoesNotExist(
                    f"No association found for {user} and {allocation}"
                )

            if len(associations) > 1:
                for association in associations[1:]:
                    logger.info(
                        f"Deleting duplicate association {association} for {user} and {allocation}"
                    )
                    association.delete()

            return first_association
        else:
            logger.error(
                f"No associations found for {user} and {allocation} after deletion"
            )
            raise models.RemoteAssociation.DoesNotExist(
                f"No association found for {user} and {allocation}"
            )


def fix_total_allocation(project):
    """
    Run this function to fix the balance of managed projects so that
    their balance at the beginning of the month is correct. This fixes
    any issues or discrepancies that may have arisen.
    """
    try:
        managed_project = models.ManagedProject.objects.get(project=project)
    except models.ManagedProject.DoesNotExist:
        logger.error(f"Project {project} is not a managed project")
        return

    project_template = managed_project.get_project_template()

    if project_template is None:
        logger.error(f"Managed project {project} has no project template")
        return

    details = managed_project.get_details()

    if details is None:
        logger.error(f"Managed project {project} has no details")
        return

    allocation = project_template.convert_to_credits(details.allocation)

    set_project_credits(project, allocation, silent=True)


def infer_allocation_from_accounting(
    project, silent: bool = False
) -> decimal.Decimal | None:
    """
    Derive what the remote award allocation should be, based solely on the
    current accounting state (project credit balance + historical spend).

    This is the inverse of fix_total_allocation: rather than updating the
    accounting to match a known award, it works backwards from the accounting
    to calculate the award value that the accounting is consistent with.

    Returns the implied allocation size in the same units as the current award
    (e.g. node-hours), or None if it cannot be determined.

    Useful when you need to push a corrected allocation back to the remote
    portal to bring the award into line with what Waldur's books reflect.
    """
    try:
        managed_project = models.ManagedProject.objects.get(project=project)
    except models.ManagedProject.DoesNotExist:
        logger.error(f"Project {project} is not a managed project")
        return None

    project_template = managed_project.get_project_template()
    if project_template is None:
        logger.error(f"Managed project {project} has no project template")
        return None

    details = managed_project.get_details()
    if details is None:
        logger.error(f"Managed project {project} has no details")
        return None

    current_allocation = details.allocation
    if current_allocation is None:
        logger.error(f"Managed project {project} has no allocation in details")
        return None

    # get the current spend information
    (allocation_credits, total_spend) = get_project_spend_info(
        project, silent=silent, include_current_month=False
    )

    # now get the project's starting month balance
    try:
        project_credit = invoice_models.ProjectCredit.objects.get(project=project)
        month_start_balance = project_credit.value
    except invoice_models.ProjectCredit.DoesNotExist:
        month_start_balance = decimal.Decimal(0.0)

    # The allocation should equal the starting month balance plus the total spend
    # (excluding current month spend)
    implied_credits = month_start_balance + total_spend

    if abs(implied_credits - allocation_credits) > decimal.Decimal(1.0):
        logger.warning(
            f"Implied credits ({implied_credits}) for project {project} differ from current allocation credits ({allocation_credits}) by more than 1.0. "
            f"This may indicate a discrepancy in the accounting data."
        )

    units = current_allocation.units
    if units in project_template.allocation_units_mapping:
        scale_factor = project_template.allocation_units_mapping[units]
        if scale_factor <= 0:
            logger.warning(
                f"Non-positive scale factor ({scale_factor}) for units {units!r} "
                f"on project {project}; cannot infer allocation."
            )
            return None
        implied_size = decimal.Decimal(str(scale_factor)) * implied_credits
    else:
        logger.warning(
            f"Units {units!r} not in allocation_units_mapping for project {project}; "
            f"treating as 1:1 (credits == allocation units)."
        )
        implied_size = implied_credits

    if not silent:
        logger.info(
            f"Implied allocation for project {project}: {implied_size} {units} "
            f"(implied_credits={implied_credits}, scale_factor="
            f"{project_template.allocation_units_mapping.get(units, 1)})"
        )

    return implied_size


def get_project_spend_info(
    project,
    include_current_month: bool = True,
    silent: bool = False,
) -> tuple[decimal.Decimal, decimal.Decimal]:
    """
    Return a tuple of the total credits and total spend for the passed project

    Args:
        project: The project to get spend info for
        include_current_month: If False, exclude current month's invoice items from spend calculation
        silent: If True, suppress logging
    """
    if not isinstance(project, structure_models.Project):
        raise TypeError("project must be an instance of Project")

    total_credits = decimal.Decimal(0.0)
    total_spend = decimal.Decimal(0.0)

    # Get all the allocations for the project
    try:
        project_credit = invoice_models.ProjectCredit.objects.get(project=project)
        total_credits = project_credit.value if project_credit else decimal.Decimal(0.0)
    except invoice_models.ProjectCredit.DoesNotExist:
        total_credits = decimal.Decimal(0.0)

    # Get all the spend for the project
    try:
        invoice_items = invoice_models.InvoiceItem.objects.filter(project=project)

        # Exclude current month if requested
        if not include_current_month:
            now = timezone.now()
            current_year = now.year
            current_month = now.month

            invoice_items = invoice_items.exclude(
                invoice__year=current_year, invoice__month=current_month
            )
    except Exception as e:
        logger.error(f"Failed to get invoice items for project {project}: {e}")
        invoice_items = []

    for invoice_item in invoice_items:
        usage = decimal.Decimal(invoice_item.price)

        if usage < 0:
            # this is a credit, so add it to the total credits
            total_credits += abs(usage)
        elif usage > 0:
            # this is a charge, so add it to the total spend
            total_spend += usage

        # no need to do anything if usage == 0

    if not silent:
        logger.debug(
            f"Project {project} has total credits: {total_credits}, total spend: {total_spend} "
            f"(include_current_month={include_current_month})"
        )

    return (total_credits, total_spend)


def get_project_credits(project, silent: bool = False) -> decimal.Decimal:
    """
    Get the total lifetime credits awarded to the project.
    If the project has no credits, return 0.0
    """
    if not isinstance(project, structure_models.Project):
        raise TypeError("project must be an instance of Project")

    (total_credits, total_spend) = get_project_spend_info(
        project, include_current_month=False, silent=silent
    )

    return total_credits + total_spend


def set_project_credits(
    project, credits: decimal.Decimal | float, silent: bool = False
):
    """
    Set the credits for the project to the passed value
    """
    if not isinstance(project, structure_models.Project):
        raise TypeError("project must be an instance of Project")

    try:
        credits = decimal.Decimal(credits)
    except (ValueError, TypeError):
        logger.error(f"Invalid credits value: {credits} for project {project}")
        raise ValueError(f"Invalid credits value: {credits} for project {project}")

    if project.is_expired or project.is_removed:
        logger.warning(
            f"Cannot set credits for project {project} as it is expired or removed"
        )
        raise ValueError(
            f"Cannot set credits for project {project} as it is expired or removed"
        )

    if credits < decimal.Decimal(0.0):
        credits = decimal.Decimal(0.0)

    # Get spend info excluding current month (we want start-of-month balance)
    (total_credits, total_spend) = get_project_spend_info(
        project, include_current_month=False, silent=silent
    )

    # Calculate what the credit balance should be: allocation - historical_spend
    # This gives us the start-of-month balance for the current month
    desired_credit_balance = credits - total_spend

    if desired_credit_balance < decimal.Decimal(0):
        logger.warning(
            f"Desired credit balance ({desired_credit_balance}) is negative for project {project}. "
            f"Allocation: {credits}, Spend: {total_spend}. "
            f"Setting balance to 0."
        )
        desired_credit_balance = decimal.Decimal(0)

    change_in_credits = desired_credit_balance - total_credits

    if abs(change_in_credits) < decimal.Decimal(0.01):
        # no change in credits, so nothing to do
        if not silent:
            logger.debug(
                f"No change in credits for project {project}: {total_credits} -> {desired_credit_balance} "
                f"(allocation: {credits}, spend: {total_spend})"
            )
        return

    if change_in_credits > decimal.Decimal(0):
        # Increasing credits -
        # Get the CustomerCredit and make sure that it has enough credits itself...
        customer_credit, created = invoice_models.CustomerCredit.objects.get_or_create(
            customer=project.customer,
        )

        remaining_customer_credit = (
            customer_credit.value - customer_credit.allocated_to_projects
        )

        # We need a little more than the change just for safety
        needed_customer_credit = change_in_credits + decimal.Decimal(0.1)

        if remaining_customer_credit < needed_customer_credit:
            logger.warning(
                f"Not enough customer credit to allocate {change_in_credits} to project {project}. "
                f"Remaining customer credit is {remaining_customer_credit} while "
                f"we need {needed_customer_credit}."
            )

            customer_credit.value = (
                customer_credit.value
                + needed_customer_credit
                - remaining_customer_credit
            )
            customer_credit.save(update_fields=["value"])
            logger.warning(
                f"Customer credit for {project.customer} increased to {customer_credit.value}"
            )

    # Now set the credits, retrying once if the save fails due to insufficient
    # customer credit (the allocated_to_projects field may be stale at pre-check
    # time, causing the save to fail even after the pre-check top-up above).
    project_credit, _created = invoice_models.ProjectCredit.objects.get_or_create(
        project=project,
    )

    for attempt in range(2):
        try:
            project_credit.value = desired_credit_balance
            project_credit.save(update_fields=["value"])
            if not silent:
                logger.info(
                    f"Project credits for project {project} set to {project_credit.value} (was {total_credits}, "
                    f"allocation: {credits}, spend: {total_spend}, change: {change_in_credits})"
                )
            return
        except Exception as e:
            if attempt == 0:
                logger.warning(
                    f"Failed to set project credits for project {project} on first attempt: {e}. "
                    f"Topping up customer credit and retrying."
                )
                customer_credit, _ = (
                    invoice_models.CustomerCredit.objects.get_or_create(
                        customer=project.customer,
                    )
                )
                # Re-fetch from DB to get current values, avoiding stale cache
                customer_credit.refresh_from_db()
                remaining = (
                    customer_credit.value - customer_credit.allocated_to_projects
                )
                shortfall = change_in_credits - remaining + decimal.Decimal("0.1")
                if shortfall > decimal.Decimal(0):
                    customer_credit.value += shortfall
                    customer_credit.save(update_fields=["value"])
                    logger.warning(
                        f"Customer credit for {project.customer} topped up by {shortfall} "
                        f"to {customer_credit.value} before retry."
                    )
            else:
                logger.error(
                    f"Failed to set project credits for project {project} after retry: {e}"
                )
                raise


def get_project_members(project) -> dict[str, str]:
    """
    Return a dictionary of all of the current members of the project,
    (email addresses) and their current roles.
    """
    if not isinstance(project, structure_models.Project):
        raise TypeError("project must be an instance of Project")

    members = {}

    project_user_ids = get_project_users(project.id)

    users = core_models.User.objects.filter(id__in=project_user_ids)

    for user in users:
        if not user.is_active:
            continue

        if user.email is None:
            continue

        email = str(user.email).strip().lower()

        if len(email) == 0:
            continue

        try:
            permission = get_permissions(project, user).first()
        except Exception:
            continue

        if permission is None:
            continue

        if permission.role is None:
            continue

        if permission.role.name is None:
            continue

        members[email] = str(permission.role.name)

    from django.contrib.contenttypes.models import ContentType

    invitations = user_models.Invitation.objects.filter(
        state=InvitationState.PENDING,
        content_type=ContentType.objects.get_for_model(project),
        object_id=project.pk,
    )

    for invite in invitations:
        if invite.email is None:
            continue

        email = str(invite.email).strip().lower()

        if len(email) == 0:
            continue

        if invite.role is None:
            continue

        if invite.role.name is None:
            continue

        if email in members:
            # already a member, so skip
            continue

        members[email] = str(invite.role.name)

    logger.debug(f"Current members of project {project}: {members}")

    return members


def invite_user_to_project(project, email, role, send_email: bool = True):
    """
    Invite a user to the project with the specified email and role.
    If a matching pending invitation already exists, do nothing.
    If send_email is True, send an invitation email.
    """
    if not isinstance(project, structure_models.Project):
        raise TypeError("project must be an instance of Project")

    if not isinstance(email, str) or not email:
        raise ValueError("email must be a non-empty string")

    duplicates = get_invitation_duplicates(project, [{"email": email, "role": role}])
    if duplicates:
        logger.info(
            "Skipping invitation for %s to project %s with role %s: "
            "pending invitation %s already exists.",
            email,
            project,
            role,
            duplicates[0]["existing_invitation_uuid"],
        )
        return

    invitation = user_models.Invitation.objects.create(
        scope=project,
        email=email,
        role=role,
        created_by=utils.get_openportal_robot(),
        state=InvitationState.PENDING,
        customer=project.customer,
    )

    if project.start_date and project.start_date > timezone.now().date():
        invitation.state = InvitationState.PENDING_PROJECT

    invitation.save()

    logger.info(
        f"Created invitation {invitation} for user {email} to project {project} with role {role}"
    )

    if send_email:
        user_tasks.process_invitation.delay(invitation.uuid.hex, "OpenPortal")


def get_or_create_user_by_email(email: str) -> core_models.User:
    """
    Return the User with the given email, creating one if none exists.

    New users are created with email and username both set to the lower-cased
    email, first_name and last_name set to "UNKNOWN" (overwritten on first login
    by the identity provider), and an unusable password.
    """
    email = email.strip().lower()
    # all_objects, not objects: the default manager hides inactive accounts, so
    # looking through it would miss a deactivated user and then fail to create a
    # replacement, because username is unique and already taken by that account.
    user = core_models.User.all_objects.filter(email__iexact=email).first()
    if user is not None:
        return user

    user = core_models.User(
        username=email,
        email=email,
        first_name="UNKNOWN",
        last_name="UNKNOWN",
    )
    user.set_unusable_password()
    user.save()
    logger.info(f"Created new user with email '{email}'.")
    return user


def set_project_member_role(project, email, role, is_existing_member: bool = False):
    """
    Directly add or update a user's role in a project without sending an invitation.

    If is_existing_member is True, the user already has an active role on the project:
    all their current active roles are revoked before the new one is granted.

    If is_existing_member is False, the user is looked up (or created) by email and
    added directly.

    Raises ValidationError if the role cannot be granted. This path bypasses the
    serializer, so it validates the same invariants explicitly, as
    Invitation.accept does for the invitation path.
    """
    if not isinstance(project, structure_models.Project):
        raise TypeError("project must be an instance of Project")

    email = str(email).strip().lower()
    robot = get_openportal_robot()

    user = get_or_create_user_by_email(email)

    if is_existing_member:
        for perm in get_permissions(project, user):
            perm.revoke(current_user=robot)

    validate_role_grant(project, user, role)
    grant_role(project, user, role, created_by=robot)
    logger.debug(f"Set role {role.name} for {email} on project {project}.")


def remove_project_member(project, email: str) -> None:
    """
    Revoke all active roles and cancel any pending invitations for the user
    with *email* on *project*.  Does nothing if no such user or membership exists.
    """
    from django.contrib.contenttypes.models import ContentType

    if not isinstance(project, structure_models.Project):
        raise TypeError("project must be an instance of Project")

    email = str(email).strip().lower()
    robot = get_openportal_robot()

    # all_objects, not objects: deactivating a user already revokes their roles,
    # so this is normally moot, but the removal should not depend on that.
    user = core_models.User.all_objects.filter(email__iexact=email).first()
    if user is not None:
        for perm in get_permissions(project, user):
            perm.revoke(current_user=robot)

    user_models.Invitation.objects.filter(
        state=InvitationState.PENDING,
        content_type=ContentType.objects.get_for_model(project),
        object_id=project.pk,
        email__iexact=email,
    ).update(state=InvitationState.CANCELED)

    logger.debug(f"Removed {email} from project {project}.")


def get_local_project_identifier(project):
    """
    Return the local openportal.ProjectIdentifier for the passed project on
    this portal. This is a combination of its openportal project name and the
    portal name, e.g. "project.portal"
    """
    shortname = get_project_shortname(project)
    if shortname is None:
        logger.error(f"Project {project} has no shortname; cannot get identifier.")
        raise ValueError(f"Project {project} has no shortname; cannot get identifier.")

    import openportal

    if not config.ensure_config_loaded():
        logger.error("OpenPortal is not configured; cannot get project identifier.")
        raise RuntimeError(
            "OpenPortal is not configured; cannot get project identifier."
        )

    config.ensure_config_loaded()

    return openportal.ProjectIdentifier(f"{shortname}.{openportal.get_portal()}")


def refresh_remote_project(remote_project):
    """
    Fetch the current AwardDetails for *remote_project* from the remote portal,
    update last_confirmed_details and reconcile allocation, then record last contact.

    Returns the AwardDetails on success, or None if OpenPortal is not configured
    or the fetch fails.
    """
    import openportal

    from . import remote_project_service
    from .board import OpenPortalBoard

    if remote_project.current_project is None:
        logger.warning(
            f"refresh_remote_project: RemoteProject {remote_project.destination!r} "
            f"has no current_project — skipping."
        )
        return None

    if not config.ensure_config_loaded():
        logger.warning("refresh_remote_project: OpenPortal is not configured.")
        return None

    config.ensure_config_loaded()

    destination = openportal.Destination(str(remote_project.destination))

    try:
        local_id = get_local_project_identifier(remote_project.current_project)
    except Exception as e:
        logger.warning(
            f"refresh_remote_project: could not get local identifier for project "
            f"{remote_project.current_project!r}: {e}"
        )
        return remote_project.last_confirmed_details

    try:
        board = OpenPortalBoard(destination)

        try:
            details = board.refetch_award(local_id)
        except exceptions.OpenPortalUnsupportedCommandError as e:
            logger.warning(
                f"refresh_remote_project: remote portal does not support get_award"
                f" for {remote_project.identifier!r} (older portal) — skipping refresh: {e}"
            )
            remote_project_service.touch_last_contact(remote_project)
            return remote_project.last_confirmed_details

        confirmed_details_json = json.loads(details.to_json()) if details else None

        if confirmed_details_json is not None:
            remote_project.last_confirmed_details = confirmed_details_json
            remote_project_service.reconcile_allocation(remote_project)
            remote_project.notes = remote_project_service._merge_notes(remote_project)
            remote_project_service._sync_project_link(remote_project)
            remote_project.save(
                update_fields=[
                    "last_confirmed_details",
                    "current_allocation",
                    "pending_allocation",
                    "notes",
                    "link_project",
                    "modified",
                ]
            )
    except Exception as e:
        logger.warning(
            f"refresh_remote_project: could not refetch award "
            f"{remote_project.identifier!r} from {remote_project.destination!r}: {e}"
        )
        remote_project_service.touch_last_contact(remote_project)
        return None

    remote_project_service.touch_last_contact(remote_project)
    return details


def _sync_project_users_from_remote(
    project: structure_models.Project,
    dry_run: bool = True,
) -> None:
    """
    Fetch the current award from every active RemoteProject linked to *project*
    and add any members that exist on the remote portal but not locally.

    Role names returned by the remote portal are expected to match the local
    Role.name (or Role.description).  If no matching Role is found the user is
    skipped and an error is logged.  Users that are already project members are
    left untouched.

    When dry_run=True (the default) the function logs what it would do but
    makes no changes to users or project membership.
    """
    from . import models as op_models

    remote_projects = (
        op_models.RemoteProject.objects.filter(
            current_project=project,
            state__in=[
                op_models.RemoteProjectState.ACTIVE,
                op_models.RemoteProjectState.STALE,
            ],
        )
        .exclude(identifier__isnull=True)
        .exclude(identifier="")
    )

    if not remote_projects.exists():
        logger.debug(
            f"No active RemoteProjects found for project {project} — skipping sync."
        )
        return

    robot = get_openportal_robot()
    current_members = get_project_members(project)
    prefix = "[DRY RUN] " if dry_run else ""

    for remote_project in remote_projects:
        logger.info(
            f"{prefix}Syncing members from remote portal {remote_project.destination} "
            f"for project {project}."
        )
        details = refresh_remote_project(remote_project)
        if details is None:
            logger.error(
                f"Failed to fetch award from {remote_project.destination} "
                f"for project {project}."
            )
            continue

        if details.members is None:
            logger.debug(
                f"Remote award for {remote_project.identifier} has no member list."
            )
            continue

        for email, role_name in details.members.items():
            email = str(email).strip().lower()

            if email in current_members:
                continue

            role = (
                Role.objects.filter(name=role_name).first()
                or Role.objects.filter(description=role_name).first()
            )
            if role is None:
                logger.error(
                    f"Role '{role_name}' not found on local portal — "
                    f"skipping member {email} from {remote_project.destination}."
                )
                continue

            logger.info(
                f"{prefix}Would add {email} to project {project} with role {role.name}."
            )

            if not dry_run:
                user = get_or_create_user_by_email(email)
                grant_role(project, user, role, created_by=robot)
                current_members[email] = role.name


def sync_users_from_remote_portal(
    scope: structure_models.Project | structure_models.Customer,
    dry_run: bool = True,
) -> None:
    """
    Sync project membership from remote portal(s) to the local portal.

    Pass a Project to sync that project only, or a Customer to sync all of
    its projects.  Members already present locally are left untouched; only
    users missing from the local project are added.

    dry_run=True (the default) logs intended changes without applying them.
    Pass dry_run=False to apply changes.
    """
    if isinstance(scope, structure_models.Project):
        _sync_project_users_from_remote(scope, dry_run=dry_run)
    elif isinstance(scope, structure_models.Customer):
        for project in structure_models.Project.objects.filter(customer=scope):
            _sync_project_users_from_remote(project, dry_run=dry_run)
    else:
        raise TypeError(f"Expected Project or Customer, got {type(scope)}")


def get_allowed_domains_for_project(project) -> list[str]:
    """
    Build an allowed_domains list from the current project members.

    For institutional addresses the domain and *.domain are added.
    For personal addresses (gmail, etc.) the full email is added instead,
    so the individual is permitted without opening the whole consumer domain.

    Returns a sorted, deduplicated list.
    """
    allowed: set[str] = set()
    for user_id in get_project_users(project.id):
        user = core_models.User.objects.filter(id=user_id, is_active=True).first()
        if user is None:
            continue
        email = (user.email or "").strip().lower()
        if not email or "@" not in email:
            continue
        if is_likely_personal_email_address(email):
            allowed.add(email)
        else:
            domain = email.split("@")[-1]
            if domain:
                allowed.add(domain)
                allowed.add(f"*.{domain}")
    return sorted(allowed)


def set_membership_control(
    remote_project,
    new_control: str,
    dry_run: bool = True,
    performed_by=None,
) -> None:
    """
    Set the membership control on *remote_project* to *new_control*
    (a MembershipControlChoices value).

    If the control has not changed, does nothing.

    When the change makes membership non-changeable by the remote portal
    (old can_change_membership() == True, new can_change_membership() == False),
    the remote portal's current member list is first synced into the local
    project and allowed_domains is rebuilt — merging with any already-permitted
    domains so nothing is lost — before the lock is applied.

    An audit entry is recorded after the change.  Pass performed_by to
    attribute the change to a specific user.

    dry_run=True (default) logs intended changes without applying them.
    The function is idempotent — re-running after a partial failure is safe.
    """
    import openportal

    from . import models as op_models
    from . import tasks as op_tasks

    if new_control is None:
        new_control = op_models.MembershipControlChoices.OPEN

    prefix = "[DRY RUN] " if dry_run else ""

    if remote_project.membership_control == new_control:
        logger.info(
            f"{prefix}{remote_project}: membership_control already {new_control!r} — nothing to do."
        )
        return

    project = remote_project.current_project
    if project is None:
        logger.error(
            f"RemoteProject {remote_project} has no current_project — cannot change membership control."
        )
        return

    # Determine whether a remote-sync is needed before applying the new control.
    # Sync is required when the remote portal was free to change membership
    # (old can_change_membership() == True) and will no longer be after the
    # transition (new can_change_membership() == False).
    try:
        old_details = remote_project.award_details()
        new_details_temp = openportal.AwardDetails.from_json(
            json.dumps({"membership_control": new_control})
        )
        needs_sync = (
            old_details.can_change_membership()
            and not new_details_temp.can_change_membership()
        )
    except Exception as e:
        logger.error(
            f"set_membership_control: failed to evaluate change for {remote_project}: {e}"
        )
        return

    logger.info(
        f"{prefix}Changing membership_control "
        f"from {remote_project.membership_control!r} to {new_control!r} "
        f"on {remote_project}" + (" (sync required)" if needs_sync else "") + "."
    )

    allowed_domains = None

    if needs_sync:
        logger.info(
            f"{prefix}Syncing users from remote portal before applying new control."
        )
        _sync_project_users_from_remote(project, dry_run=dry_run)

        new_domains = {str(d) for d in get_allowed_domains_for_project(project)}
        existing_domains = {str(d) for d in (remote_project.allowed_domains or [])}
        allowed_domains = sorted(existing_domains | new_domains)
        logger.info(f"{prefix}Computed allowed_domains: {allowed_domains}")

    if remote_project.allowed_domains is not None and allowed_domains is None:
        # make sure we capture any pre-existing allowed_domains if we're not
        # already doing a sync (which will compute the merged set)
        allowed_domains = remote_project.allowed_domains

    if dry_run:
        msg = f"Would set membership_control={new_control!r}"
        if allowed_domains is not None:
            msg += f", allowed_domains={allowed_domains}"
        logger.info(f"{prefix}{msg}, and push update.")
        return

    remote_project.membership_control = new_control
    save_fields = ["membership_control", "modified"]

    remote_project.allowed_domains = allowed_domains
    save_fields.append("allowed_domains")

    remote_project.save(update_fields=save_fields)

    note = f"membership_control set to {new_control!r}"
    if needs_sync:
        note += " (members synced from remote portal)"
    op_models.RemoteProjectAuditEntry.objects.create(
        remote_project=remote_project,
        event_type=op_models.RemoteProjectAuditEventType.AWARD_UPDATED,
        performed_by=performed_by,
        note=note,
    )

    op_tasks.update_remote_project.delay(core_utils.serialize_instance(project))
    logger.info(f"Queued update_remote_project for {project}.")


def _user_info_dict(user):
    return {
        "uuid": str(user.uuid),
        "full_name": user.full_name,
        "username": user.username,
        "email": user.email,
    }


def _resolve_useridentifier_from_slugs(identifier: str):
    """
    Fallback resolver when the Association record has been deleted.

    A UserIdentifier has the form "{user_slug}.{project_slug}.{portal}".
    We verify the portal suffix matches, then look up the project and user
    by their slugs and confirm the user is still a member of that project.
    Returns a user info dict or None.
    """
    import openportal

    from waldur_core.permissions.models import UserRole
    from waldur_core.structure import models as structure_models

    try:
        portal = str(openportal.get_portal())
    except Exception:
        return None

    portal_suffix = f".{portal}"
    if not identifier.endswith(portal_suffix):
        return None

    remainder = identifier[: -len(portal_suffix)]

    # remainder is "{user_slug}.{project_slug}" — split from the right once
    parts = remainder.rsplit(".", 1)
    if len(parts) != 2:
        return None

    user_slug, project_slug = parts

    try:
        project = structure_models.Project.objects.get(slug=project_slug)
    except structure_models.Project.DoesNotExist:
        return None

    try:
        user = structure_models.User.objects.get(slug=user_slug)
    except structure_models.User.DoesNotExist:
        return None

    if not UserRole.objects.filter(user=user, scope=project, is_active=True).exists():
        return None

    return _user_info_dict(user)


def resolve_emails(emails: list) -> dict:
    """
    Map email address strings to Waldur user info dicts.

    Used for cached reports from remote portals, where the user_mapping field
    contains email addresses rather than UserIdentifier strings.

    Returns dict of {email_string: {"uuid", "email", "full_name", "username"}}
    Emails that do not match any Waldur user map to None.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    users = {u.email: u for u in User.objects.filter(email__in=emails)}
    return {
        email: (_user_info_dict(users[email]) if email in users else None)
        for email in emails
    }


def resolve_useridentifiers(identifiers: list) -> dict:
    """
    Map OpenPortal UserIdentifier strings to Waldur user info dicts.

    Primary chain: Association.useridentifier == identifier -> Association.user
    Fallback (when Association is deleted): parse the identifier as
    "{user_slug}.{project_slug}.{portal}" and verify the user is still a
    member of the project.

    Returns dict of {identifier_string: {"uuid", "email", "full_name", "username"}}
    Missing or unmapped identifiers map to None.
    """
    result = {}
    for identifier in identifiers:
        association = (
            models.Association.objects.filter(useridentifier=identifier)
            .select_related("user")
            .first()
        )

        if association is not None and association.user is not None:
            result[identifier] = _user_info_dict(association.user)
            continue

        # Association missing or user deleted — try slug-based fallback
        result[identifier] = _resolve_useridentifier_from_slugs(identifier)

    return result


def backfill_usage_report_cache():
    """
    Backfill CachedProjectUsageReport for all historical months.

    Run this once manually after deploying to populate the cache for months
    prior to the current one. The regular sync_usage task handles the current
    (and previous) month going forward, so this only fetches up to and
    including the month before the current one.

    Covers all allocations that ever had an OpenPortal project identifier,
    including those on inactive or soft-deleted projects, so the full
    historical record is preserved.

    A project's history starts at its start_date (falling back to its
    created date if start_date is not set), and ends at the earlier of:
      - the month before the current month, or
      - the month it expired / passed its grace period end date.

    Skips any month for which a complete CachedProjectUsageReport already
    exists, so it is safe to re-run.
    """
    import openportal

    from .backend import OpenPortalBackend

    today = timezone.now().date()
    # Last day of the previous month — we backfill up to and including this
    first_of_current = today.replace(day=1)
    last_historical = first_of_current - datetime.timedelta(days=1)

    all_allocations = list(models.Allocation.objects.all().select_related("project"))

    total_fetched = 0
    total_skipped = 0
    total_errors = 0

    for allocation in all_allocations:
        project = allocation.project

        if not allocation.has_project_identifier():
            logger.debug(f"backfill: skipping {allocation} - no project identifier")
            continue

        # Determine the start month for this allocation
        start_date = project.start_date or project.created.date()
        start_month_first = start_date.replace(day=1)

        # Determine the end month (don't go past last_historical)
        if project.end_date_with_grace:
            project_end = min(project.end_date_with_grace, last_historical)
        else:
            project_end = last_historical

        if start_month_first > project_end:
            logger.debug(
                f"backfill: skipping {allocation} - no historical months to fill"
            )
            continue

        try:
            backend = OpenPortalBackend(allocation.service_settings)
        except Exception as e:
            logger.error(f"backfill: could not create backend for {allocation}: {e}")
            total_errors += 1
            continue

        resource = str(backend.client.destination())
        project_identifier = str(allocation.get_project_identifier())

        # Walk month by month from start_month_first to the end month
        cursor = start_month_first
        while cursor <= project_end:
            year, month = cursor.year, cursor.month
            last_day = calendar.monthrange(year, month)[1]
            month_end = cursor.replace(day=last_day)

            logger.info(
                f"backfill: processing {project_identifier} {year}-{month:02d} ({resource})"
            )

            # Skip if we already have a complete cached report for this month
            already_complete = models.CachedProjectUsageReport.objects.filter(
                year=year,
                month=month,
                project_identifier=project_identifier,
                resource=resource,
                is_complete=True,
            ).exists()

            if already_complete:
                logger.debug(
                    f"backfill: skipping {project_identifier} {year}-{month:02d} "
                    f"({resource}) - already complete"
                )
                total_skipped += 1
            else:
                try:
                    date_range = openportal.DateRange(cursor, month_end)

                    # should try to get the report 10 times, as sometimes this
                    # can take OpenPortal a while if there are lots of jobs that month
                    report = None

                    for attempt in range(10):
                        try:
                            report = backend.client.get_usage_report(
                                allocation.get_project_identifier(), date_range
                            )
                            break
                        except Exception as e:
                            logger.warning(
                                f"backfill: attempt {attempt + 1} - failed to fetch report for "
                                f"{project_identifier} {year}-{month:02d} ({resource}): {e}"
                            )

                    if report is None:
                        logger.error(
                            f"backfill: failed to fetch report for {project_identifier} "
                            f"{year}-{month:02d} ({resource}) after 10 attempts"
                        )
                        total_errors += 1
                        continue

                    models.CachedProjectUsageReport.objects.update_or_create(
                        year=year,
                        month=month,
                        project_identifier=project_identifier,
                        resource=resource,
                        defaults={
                            "is_complete": True,
                            "report": json.loads(report.to_json()),
                        },
                    )
                    logger.info(
                        f"backfill: cached {project_identifier} {year}-{month:02d} ({resource})"
                    )
                    total_fetched += 1
                except Exception as e:
                    logger.error(
                        f"backfill: failed for {project_identifier} {year}-{month:02d}: {e}"
                    )
                    total_errors += 1

            # Advance to the first of the next month
            if month == 12:
                cursor = cursor.replace(year=year + 1, month=1, day=1)
            else:
                cursor = cursor.replace(month=month + 1, day=1)

    logger.info(
        f"backfill_usage_report_cache complete: "
        f"{total_fetched} fetched, {total_skipped} skipped, {total_errors} errors"
    )
    return {
        "fetched": total_fetched,
        "skipped": total_skipped,
        "errors": total_errors,
    }


def compare_historical_usage_with_cache():
    """
    Compare historical monthly node-hour consumption recorded in HistoricalAllocation
    against the totals from CachedProjectUsageReport for the same months.

    This is useful for understanding the impact of changes to node-hour accounting
    (e.g. rounding up to the next highest node second) on previously recorded usage.

    Only complete months are compared (is_complete=True on both sides). Months
    where no CachedProjectUsageReport exists are skipped.

    Returns a dict keyed by project_identifier with:
        - allocation_name: human-readable name of the allocation
        - total_historical: sum of HistoricalAllocation.node_usage across all complete months
        - total_cached: sum of CachedProjectUsageReport total_usage.hours across the same months
        - total_difference: total_cached - total_historical (positive = cached is higher)
        - months: list of per-month dicts with keys:
            year, month, historical, cached, difference
    """
    results = {}

    historical_qs = (
        models.HistoricalAllocation.objects.filter(is_complete=True)
        .select_related("allocation")
        .order_by("allocation__id", "year", "month")
    )

    for hist in historical_qs:
        allocation = hist.allocation
        if not allocation.has_project_identifier():
            continue

        project_identifier = str(allocation.get_project_identifier())

        cached_reports = models.CachedProjectUsageReport.objects.filter(
            year=hist.year,
            month=hist.month,
            project_identifier=project_identifier,
            is_complete=True,
        )

        if not cached_reports.exists():
            continue

        try:
            cached_total = sum(
                float(cr.get_report().total_usage.hours) for cr in cached_reports
            )
        except Exception as e:
            logger.warning(
                f"compare_historical_usage_with_cache: could not read cached report "
                f"for {project_identifier} {hist.year}-{hist.month:02d}: {e}"
            )
            continue

        historical = float(hist.node_usage)
        difference = cached_total - historical

        if project_identifier not in results:
            results[project_identifier] = {
                "allocation_name": allocation.name,
                "total_historical": 0.0,
                "total_cached": 0.0,
                "total_difference": 0.0,
                "months": [],
            }

        entry = results[project_identifier]
        entry["total_historical"] += historical
        entry["total_cached"] += cached_total
        entry["total_difference"] += difference
        entry["months"].append(
            {
                "year": hist.year,
                "month": hist.month,
                "historical": historical,
                "cached": cached_total,
                "difference": difference,
            }
        )

    logger.info(
        f"compare_historical_usage_with_cache: compared {len(results)} projects"
    )
    return results


def backfill_remote_usage_report_cache():
    """
    Backfill CachedProjectUsageReport for all historical months using data
    fetched from remote portals via RemoteOpenPortalBackend.

    This is the remote-portal equivalent of backfill_usage_report_cache(): it
    iterates over RemoteAllocation objects and fetches usage reports from the
    remote portal for each historical month, storing them in the local
    CachedProjectUsageReport table.

    Run this once manually after deploying to populate the cache for months
    prior to the current one. The regular sync_remote_usage task handles the
    current (and previous) month going forward.

    Skips any month for which a complete CachedProjectUsageReport already
    exists, so it is safe to re-run.
    """
    import openportal

    from .remotebackend import RemoteOpenPortalBackend

    today = timezone.now().date()
    first_of_current = today.replace(day=1)
    last_historical = first_of_current - datetime.timedelta(days=1)

    all_allocations = list(
        models.RemoteAllocation.objects.all().select_related("project")
    )

    total_fetched = 0
    total_skipped = 0
    total_errors = 0

    for allocation in all_allocations:
        project = allocation.project

        if not allocation.has_project_identifier():
            logger.debug(
                f"backfill_remote: skipping {allocation} - no project identifier"
            )
            continue

        start_date = project.start_date or project.created.date()
        start_month_first = start_date.replace(day=1)

        if project.end_date_with_grace:
            project_end = min(project.end_date_with_grace, last_historical)
        else:
            project_end = last_historical

        if start_month_first > project_end:
            logger.debug(
                f"backfill_remote: skipping {allocation} - no historical months to fill"
            )
            continue

        try:
            backend = RemoteOpenPortalBackend(allocation.service_settings)
        except Exception as e:
            logger.error(
                f"backfill_remote: could not create backend for {allocation}: {e}"
            )
            total_errors += 1
            continue

        resource = str(backend.client.destination())
        project_identifier = str(allocation.get_project_identifier())

        cursor = start_month_first
        while cursor <= project_end:
            year, month = cursor.year, cursor.month
            last_day = calendar.monthrange(year, month)[1]
            month_end = cursor.replace(day=last_day)

            logger.info(
                f"backfill_remote: processing {project_identifier} "
                f"{year}-{month:02d} ({resource})"
            )

            already_complete = models.CachedProjectUsageReport.objects.filter(
                year=year,
                month=month,
                project_identifier=project_identifier,
                resource=resource,
                is_complete=True,
            ).exists()

            if already_complete:
                logger.debug(
                    f"backfill_remote: skipping {project_identifier} "
                    f"{year}-{month:02d} ({resource}) - already complete"
                )
                total_skipped += 1
            else:
                try:
                    date_range = openportal.DateRange(cursor, month_end)

                    report = None
                    for attempt in range(10):
                        try:
                            report = backend.client.get_usage_report(
                                allocation.get_project_identifier(), date_range
                            )
                            break
                        except Exception as e:
                            logger.warning(
                                f"backfill_remote: attempt {attempt + 1} - failed to fetch "
                                f"report for {project_identifier} {year}-{month:02d}: {e}"
                            )

                    if report is None:
                        logger.error(
                            f"backfill_remote: failed to fetch report for "
                            f"{project_identifier} {year}-{month:02d} after 10 attempts"
                        )
                        total_errors += 1
                    else:
                        models.CachedProjectUsageReport.objects.update_or_create(
                            year=year,
                            month=month,
                            project_identifier=project_identifier,
                            resource=resource,
                            defaults={
                                "is_complete": True,
                                "report": json.loads(report.to_json()),
                            },
                        )
                        logger.info(
                            f"backfill_remote: cached {project_identifier} "
                            f"{year}-{month:02d} ({resource})"
                        )
                        total_fetched += 1
                except Exception as e:
                    logger.error(
                        f"backfill_remote: failed for {project_identifier} "
                        f"{year}-{month:02d}: {e}"
                    )
                    total_errors += 1

            if month == 12:
                cursor = cursor.replace(year=year + 1, month=1, day=1)
            else:
                cursor = cursor.replace(month=month + 1, day=1)

    logger.info(
        f"backfill_remote_usage_report_cache complete: "
        f"{total_fetched} fetched, {total_skipped} skipped, {total_errors} errors"
    )
    return {
        "fetched": total_fetched,
        "skipped": total_skipped,
        "errors": total_errors,
    }


def compare_historical_remote_usage_with_cache():
    """
    Compare historical monthly node-hour consumption recorded in HistoricalRemoteAllocation
    against the totals from CachedProjectUsageReport for the same months.

    This is the remote-portal equivalent of compare_historical_usage_with_cache: it
    uses HistoricalRemoteAllocation (populated by RemoteOpenPortalBackend.sync_usage)
    and the CachedProjectUsageReport records that sync_usage now also writes.

    Only complete months are compared (is_complete=True on both sides). Months
    where no CachedProjectUsageReport exists are skipped.

    Returns a dict keyed by project_identifier with:
        - allocation_name: human-readable name of the remote allocation
        - total_historical: sum of HistoricalRemoteAllocation.node_usage across all complete months
        - total_cached: sum of CachedProjectUsageReport total_usage.hours across the same months
        - total_difference: total_cached - total_historical (positive = cached is higher)
        - months: list of per-month dicts with keys:
            year, month, historical, cached, difference
    """
    results = {}

    historical_qs = (
        models.HistoricalRemoteAllocation.objects.filter(is_complete=True)
        .select_related("allocation")
        .order_by("allocation__id", "year", "month")
    )

    for hist in historical_qs:
        allocation = hist.allocation
        if not allocation.has_project_identifier():
            continue

        project_identifier = str(allocation.get_project_identifier())

        cached_reports = models.CachedProjectUsageReport.objects.filter(
            year=hist.year,
            month=hist.month,
            project_identifier=project_identifier,
            is_complete=True,
        )

        if not cached_reports.exists():
            continue

        try:
            cached_total = sum(
                float(cr.get_report().total_usage.hours) for cr in cached_reports
            )
        except Exception as e:
            logger.warning(
                f"compare_historical_remote_usage_with_cache: could not read cached report "
                f"for {project_identifier} {hist.year}-{hist.month:02d}: {e}"
            )
            continue

        historical = float(hist.node_usage)
        difference = cached_total - historical

        if project_identifier not in results:
            results[project_identifier] = {
                "allocation_name": allocation.name,
                "total_historical": 0.0,
                "total_cached": 0.0,
                "total_difference": 0.0,
                "months": [],
            }

        entry = results[project_identifier]
        entry["total_historical"] += historical
        entry["total_cached"] += cached_total
        entry["total_difference"] += difference
        entry["months"].append(
            {
                "year": hist.year,
                "month": hist.month,
                "historical": historical,
                "cached": cached_total,
                "difference": difference,
            }
        )

    logger.info(
        f"compare_historical_remote_usage_with_cache: compared {len(results)} projects"
    )
    return results


def sync_openportal_shortnames_to_slugs():
    """
    Synchronize shortnames from ProjectInfo and UserInfo to their respective
    Project and User slug fields. This is used in "Project Management" mode
    where these shortnames are used instead of proposal IDs.

    This function:
    1. Copies ProjectInfo.shortname to Project.slug where they differ
    2. Copies UserInfo.shortname to User.slug where they differ

    Only updates slugs where:
    - The shortname exists (is not None/empty)
    - The shortname differs from the current slug

    Returns:
        dict: Statistics about the sync operation with keys:
            - projects_updated: Number of projects updated
            - users_updated: Number of users updated
            - projects_skipped: Number of projects skipped (no shortname or already matching)
            - users_skipped: Number of users skipped (no shortname or already matching)
            - errors: List of error messages encountered
    """
    projects_updated = 0
    users_updated = 0
    projects_skipped = 0
    users_skipped = 0
    errors = []

    logger.info("Starting sync of OpenPortal shortnames to slugs...")

    # Sync ProjectInfo shortnames to Project slugs
    for project_info in models.ProjectInfo.objects.all().select_related("project"):
        try:
            # Skip if no shortname or project doesn't exist
            if not project_info.shortname or not project_info.project:
                projects_skipped += 1
                logger.debug(
                    f"Skipping ProjectInfo {project_info.id}: "
                    f"shortname={project_info.shortname}, project exists={bool(project_info.project)}"
                )
                continue

            project = project_info.project
            shortname = project_info.shortname.strip()

            # Skip if slug already matches
            if project.slug == shortname:
                projects_skipped += 1
                logger.debug(
                    f"Skipping project {project.name} ({project.uuid}): "
                    f"slug already matches shortname '{shortname}'"
                )
                continue

            # Update the slug
            old_slug = project.slug
            project.slug = shortname
            project.save(update_fields=["slug"])
            projects_updated += 1

            logger.info(
                f"Updated project {project.name} ({project.uuid}) slug: "
                f"'{old_slug}' -> '{shortname}'"
            )

        except Exception as e:
            error_msg = (
                f"Failed to sync ProjectInfo {project_info.id} "
                f"(shortname='{project_info.shortname}'): {e}"
            )
            errors.append(error_msg)
            logger.error(error_msg, exc_info=True)

    # Sync UserInfo shortnames to User slugs
    for user_info in models.UserInfo.objects.all().select_related("user"):
        try:
            # Skip if no shortname or user doesn't exist
            if not user_info.shortname or not user_info.user:
                users_skipped += 1
                logger.debug(
                    f"Skipping UserInfo {user_info.id}: "
                    f"shortname={user_info.shortname}, user exists={bool(user_info.user)}"
                )
                continue

            user = user_info.user
            shortname = user_info.shortname.strip()

            # Skip if slug already matches
            if user.slug == shortname:
                users_skipped += 1
                logger.debug(
                    f"Skipping user {user.username} ({user.uuid}): "
                    f"slug already matches shortname '{shortname}'"
                )
                continue

            # Update the slug
            old_slug = user.slug
            user.slug = shortname
            user.save(update_fields=["slug"])
            users_updated += 1

            logger.info(
                f"Updated user {user.username} ({user.uuid}) slug: "
                f"'{old_slug}' -> '{shortname}'"
            )

        except Exception as e:
            error_msg = (
                f"Failed to sync UserInfo {user_info.id} "
                f"(shortname='{user_info.shortname}'): {e}"
            )
            errors.append(error_msg)
            logger.error(error_msg, exc_info=True)

    # Log summary
    logger.info(
        f"Sync complete. Projects: {projects_updated} updated, {projects_skipped} skipped. "
        f"Users: {users_updated} updated, {users_skipped} skipped. "
        f"Errors: {len(errors)}"
    )

    return {
        "projects_updated": projects_updated,
        "users_updated": users_updated,
        "projects_skipped": projects_skipped,
        "users_skipped": users_skipped,
        "errors": errors,
    }


PERSONAL_EMAIL_DOMAINS = frozenset(
    [
        # Google
        "gmail.com",
        "googlemail.com",
        # Yahoo
        "yahoo.com",
        "yahoo.co.uk",
        "yahoo.fr",
        "yahoo.de",
        "yahoo.es",
        "yahoo.it",
        "yahoo.com.au",
        "yahoo.ca",
        "yahoo.co.in",
        "ymail.com",
        # Microsoft
        "hotmail.com",
        "hotmail.co.uk",
        "hotmail.fr",
        "hotmail.de",
        "hotmail.es",
        "hotmail.it",
        "outlook.com",
        "outlook.co.uk",
        "outlook.fr",
        "live.com",
        "live.co.uk",
        "live.fr",
        "msn.com",
        # Apple
        "icloud.com",
        "me.com",
        "mac.com",
        # Proton
        "protonmail.com",
        "protonmail.ch",
        "proton.me",
        "pm.me",
        # AOL
        "aol.com",
        "aol.co.uk",
        # Other common
        "mail.com",
        "zoho.com",
        "yandex.com",
        "yandex.ru",
        "gmx.com",
        "gmx.de",
        "gmx.net",
        "web.de",
        "t-online.de",
        "tutanota.com",
        "tutanota.de",
        "fastmail.com",
        "fastmail.fm",
        "inbox.com",
        "hushmail.com",
        "mailfence.com",
        "disroot.org",
        "riseup.net",
        "posteo.de",
        "posteo.net",
        "mailbox.org",
        "cock.li",
        "seznam.cz",
        "wp.pl",
        "o2.pl",
        "interia.pl",
        "hey.com",
    ]
)


def is_likely_personal_email_address(email_or_domain: str) -> bool:
    """
    Return True if the email address or domain is a known personal /
    consumer email provider.

    Accepts either a full address ("user@gmail.com") or just the
    domain ("gmail.com").  The check is case-insensitive.

    Not exhaustive — only checks against a curated list of well-known
    personal email providers.
    """
    if "@" in email_or_domain:
        domain = email_or_domain.split("@")[-1].strip().lower()
    else:
        domain = email_or_domain.strip().lower()
    return domain in PERSONAL_EMAIL_DOMAINS


def get_project_member_domains(project) -> list:
    """
    Return a sorted list of unique email domains used by the active
    members of *project*, excluding domains that are likely personal
    email providers (gmail.com, hotmail.com, etc.).

    Returns an empty list if the project has no members with
    institutional email addresses.
    """
    domains = set()
    try:
        for user in project.get_users():
            if user.email:
                domain = user.email.split("@")[-1].strip().lower()
                if domain and not is_likely_personal_email_address(domain):
                    domains.add(domain)
    except Exception as e:
        logger.warning(f"get_project_member_domains: failed for {project}: {e}")
    return sorted(domains)


def get_proposal_links_for_project(project):
    """
    Return ``(link_award, link_call)`` dicts for *project*, or
    ``(None, None)`` if no proposal is attached.

    ``link_award`` points to the local project (the "award") in homeport:
        url  = projects/{project_uuid}/
        id   = project.slug  (set to the human-readable award ID,
               e.g. "026-235785392-1")

    ``link_call`` points to the call page in homeport:
        url  = call/{call_uuid}/
        id   = "{call.name} - {round.start_time:%Y-%m}"

    ``link_call`` is derived from the first Proposal attached to the
    project; it is None if no proposal is found.
    """
    try:
        from waldur_core.core.utils import format_homeport_link

        link_award = {
            "id": project.slug,
            "url": format_homeport_link(
                "projects/{project_uuid}/",
                project_uuid=project.uuid,
            ),
        }

        try:
            from waldur_mastermind.proposal.models import Proposal

            proposal = (
                Proposal.objects.filter(project=project)
                .select_related("round__call__manager__customer")
                .first()
            )
            if proposal is not None:
                call = proposal.round.call
                start_time = (
                    proposal.round.start_time.strftime("%Y-%m-%d")
                    if proposal.round.start_time
                    else "unknown"
                )
                end_time = (
                    proposal.round.cutoff_time.strftime("%Y-%m-%d")
                    if proposal.round.cutoff_time
                    else "unknown"
                )
                link_call = {
                    "id": f"{call.name} - round: {start_time} to {end_time}",
                    "url": format_homeport_link(
                        "calls/{call_uuid}/",
                        call_uuid=call.uuid,
                    ),
                }
            else:
                link_call = None
        except Exception as e:
            logger.warning(
                f"get_proposal_links_for_project: could not resolve"
                f" call link for {project}: {e}"
            )
            link_call = None

        return link_award, link_call

    except Exception as e:
        logger.warning(f"get_proposal_links_for_project: failed for {project}: {e}")
        return None, None


def backfill_remote_projects(dry_run: bool = False):
    """
    Create or update RemoteProject objects for all existing
    RemoteAllocations that have a project identifier set.

    This is a one-time utility for migrating existing data.  Safe to
    run multiple times — get_or_create ensures no duplicates.

    Args:
        dry_run: If True, no database writes are performed.  The return
                 value shows exactly what *would* happen, including a
                 'plan' list of per-allocation actions.

    Returns a summary dict with counts, errors, and (in dry-run mode)
    a 'plan' list of dicts describing what would be done.
    """
    from . import remote_project_service  # noqa: F401 (used when not dry)

    created_count = 0
    updated_count = 0
    skipped_count = 0
    errors = []
    plan = []  # populated in dry-run mode

    allocations = models.RemoteAllocation.objects.filter(is_active=True)

    for allocation in allocations:
        try:
            backend = allocation.get_backend()
            destination = str(backend.destination())
        except Exception as e:
            msg = (
                f"backfill_remote_projects: cannot get destination"
                f" for {allocation}: {e}"
            )
            errors.append(msg)
            logger.warning(msg)
            if dry_run:
                plan.append(
                    {
                        "allocation": str(allocation),
                        "action": "error",
                        "reason": msg,
                    }
                )
            continue

        identifier = None
        try:
            identifier = (
                str(allocation.get_remote_project_identifier())
                if allocation.has_remote_project_identifier()
                else None
            )

            if identifier is not None:
                existing = models.RemoteProject.objects.filter(
                    destination=destination,
                    identifier=identifier,
                ).first()
            else:
                existing = models.RemoteProject.objects.filter(
                    destination=destination,
                    identifier__isnull=True,
                    current_project=allocation.project,
                ).first()

            if dry_run:
                alloc_value, _ = allocation._get_requested_allocation()
                plan.append(
                    {
                        "allocation": str(allocation),
                        "identifier": identifier,
                        "destination": destination,
                        "action": "update" if existing else "create",
                        "is_added": allocation.is_added,
                        "allocation_value": (
                            float(alloc_value) if alloc_value is not None else None
                        ),
                        "current_project": (
                            str(allocation.project) if allocation.project else None
                        ),
                    }
                )
                if existing is None:
                    created_count += 1
                else:
                    updated_count += 1
                continue

            remote_project = remote_project_service.get_or_create_remote_project(
                allocation,
                destination,
                remote_identifier=identifier,
            )
            remote_project_service.ensure_current_attachment(remote_project)

            if existing is None:
                # Newly created — set state and allocation from what
                # we know about the allocation right now.  Do NOT set
                # last_contact_time: we don't know when we last heard
                # from the remote portal for this historical entry.
                #
                # Explicitly set Open so that the remote portal retains
                # full control of its own membership during migration.
                remote_project.membership_control = models.MembershipControlChoices.OPEN
                if allocation.is_added:
                    remote_project.state = models.RemoteProjectState.ACTIVE
                    alloc_value, _ = allocation._get_requested_allocation()
                    if alloc_value is not None:
                        remote_project.current_allocation = decimal.Decimal(
                            str(alloc_value)
                        )
                remote_project.save()

                models.RemoteProjectAuditEntry.objects.create(
                    remote_project=remote_project,
                    event_type=(models.RemoteProjectAuditEventType.AWARD_CREATED),
                    note=("Created by backfill_remote_projects utility."),
                )
                created_count += 1
                logger.info(
                    f"backfill_remote_projects: created RemoteProject"
                    f" {identifier} via {destination}"
                )
            else:
                updated_count += 1
                logger.debug(
                    f"backfill_remote_projects: updated RemoteProject"
                    f" {identifier} via {destination}"
                )

        except Exception as e:
            msg = (
                f"backfill_remote_projects: failed for {allocation}"
                f" ({identifier} via {destination}): {e}"
            )
            errors.append(msg)
            logger.error(msg, exc_info=True)

    prefix = "[DRY RUN] " if dry_run else ""
    logger.info(
        f"{prefix}backfill_remote_projects complete: "
        f"{created_count} created, {updated_count} updated, "
        f"{skipped_count} skipped, {len(errors)} errors"
    )

    result = {
        "dry_run": dry_run,
        "created": created_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "errors": errors,
    }
    if dry_run:
        result["plan"] = plan
    return result


def find_finished_projects():
    """
    Find projects that have finished, i.e. are removed, or past their
    grace period
    """
    all_projects = structure_models.Project.objects.all()
    finished_projects = []
    today = timezone.now().date()

    for project in all_projects:
        try:
            end_date_with_grace = project.end_date_with_grace

            if end_date_with_grace is not None and end_date_with_grace < today:
                logger.debug(
                    f"Project {project} has end date with grace {end_date_with_grace} "
                    f"which is in the past."
                )
                finished_projects.append(project)
            elif project.is_removed:
                logger.debug(f"Project {project} is removed.")
                finished_projects.append(project)
        except Exception as e:
            logger.warning(f"Failed to check if project {project} is finished: {e}")

    return finished_projects


def fix_managed_project_destinations():
    """
    One-off data-fix: correct ManagedProject destinations that were stored with
    the bridge agent name instead of the local portal name.

    Incorrect: {remote_portal}.{bridge}.{resource}  e.g. brics.waldur.isambard-ai
    Correct:   {local_portal}.{remote_portal}.{resource}  e.g. ukri.brics.isambard-ai

    The local portal is the last component of the project identifier, e.g.
    "myproject.ukri" → "ukri".  A destination is already correct when its first
    component matches the local portal, and is skipped.
    """
    fixed = 0
    skipped = 0
    errors = 0

    for mp in models.ManagedProject.objects.all():
        try:
            dest_parts = mp.destination.split(".")
            if len(dest_parts) != 3:
                logger.warning(
                    f"fix_managed_project_destinations: unexpected destination "
                    f"{mp.destination!r} for {mp} (expected 3 parts) — skipping"
                )
                skipped += 1
                continue

            # The portal is the last component of the identifier, e.g. "project.ukri" → "ukri"
            local_portal = mp.identifier.rsplit(".", 1)[-1]

            if local_portal in dest_parts:
                skipped += 1
                continue

            correct_destination = f"{local_portal}.{dest_parts[0]}.{dest_parts[2]}"

            logger.info(
                f"fix_managed_project_destinations: {mp} "
                f"{mp.destination!r} → {correct_destination!r}"
            )

            mp.destination = correct_destination
            mp.save(update_fields=["destination"])
            fixed += 1

        except Exception as e:
            logger.error(
                f"fix_managed_project_destinations: error processing {mp}: {e}"
            )
            errors += 1

    logger.info(
        f"fix_managed_project_destinations complete: "
        f"fixed={fixed}, skipped={skipped}, errors={errors}"
    )
    return fixed, skipped, errors
