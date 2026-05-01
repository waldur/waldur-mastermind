import logging

from constance import config
from django.db import transaction
from django.utils import timezone

from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.models import User
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.models import UserRole
from waldur_core.structure.permissions import _get_customer

logger = logging.getLogger(__name__)


def get_deactivation_reason(user: User) -> str | None:
    """
    Check if a user should be deactivated based on the current policy.

    Returns a reason string if the user should be deactivated, or None otherwise.

    A user should be deactivated if:
    - DEACTIVATE_USER_IF_NO_ROLES setting is enabled
    - User has no active roles
    - User has no active course accounts in non-removed projects
    - User is currently active
    - User is not staff or support
    """
    if not config.DEACTIVATE_USER_IF_NO_ROLES:
        return None

    if not user.is_active or user.is_staff or user.is_support:
        logger.debug(
            "User %s (uuid=%s) skipped for deactivation: is_active=%s, is_staff=%s, is_support=%s",
            user.username,
            user.uuid,
            user.is_active,
            user.is_staff,
            user.is_support,
        )
        return None

    if UserRole.objects.filter(user=user, is_active=True).exists():
        logger.debug(
            "User %s (uuid=%s) skipped for deactivation: has active roles",
            user.username,
            user.uuid,
        )
        return None

    from waldur_mastermind.marketplace.enums import CourseAccountState
    from waldur_mastermind.marketplace.models import CourseAccount

    ok_course_accounts = CourseAccount.objects.filter(
        user=user,
        state=CourseAccountState.OK,
        project__is_removed=False,
    )
    if ok_course_accounts.exists():
        logger.debug(
            "User %s (uuid=%s) skipped for deactivation: has %d OK course account(s) in active projects",
            user.username,
            user.uuid,
            ok_course_accounts.count(),
        )
        return None

    all_course_accounts = CourseAccount.objects.filter(user=user)
    if all_course_accounts.exists():
        ca_details = list(
            all_course_accounts.values_list(
                "uuid", "state", "project__uuid", "project__is_removed"
            )
        )
        reason = (
            f"No active roles, {all_course_accounts.count()} course account(s) "
            f"but none in OK state with active project. Details: {ca_details}"
        )
        logger.info(
            "User %s (uuid=%s) will be deactivated: %s",
            user.username,
            user.uuid,
            reason,
        )
        return reason

    reason = "No active roles and no course accounts"
    logger.info(
        "User %s (uuid=%s) will be deactivated: %s",
        user.username,
        user.uuid,
        reason,
    )
    return reason


def should_deactivate_user(user: User) -> bool:
    """Check if a user should be deactivated based on the current policy."""
    return get_deactivation_reason(user) is not None


def should_reactivate_user(user: User) -> bool:
    """
    Check if a user should be reactivated based on the current policy.

    Returns True if:
    - DEACTIVATE_USER_IF_NO_ROLES setting is enabled
    - User is currently inactive
    - User is not staff or support
    - User has active roles OR has OK course accounts in active projects
    """
    if not config.DEACTIVATE_USER_IF_NO_ROLES:
        return False

    if user.is_active or user.is_staff or user.is_support:
        return False

    if UserRole.objects.filter(user=user, is_active=True).exists():
        return True

    from waldur_mastermind.marketplace.enums import CourseAccountState
    from waldur_mastermind.marketplace.models import CourseAccount

    if CourseAccount.objects.filter(
        user=user,
        state=CourseAccountState.OK,
        project__is_removed=False,
    ).exists():
        return True

    return False


def deactivate_user_with_logging(user: User, reason: str = "No active roles") -> None:
    """
    Deactivate a user and log the action.
    """
    user.is_active = False
    user.deactivation_reason = reason
    user.save(update_fields=["is_active", "deactivation_reason"])

    logger.info(
        f"User {user} (uuid={user.uuid}) has been deactivated automatically. Reason: {reason}"
    )

    event_logger.emit(
        "User {affected_user_username} has been deactivated automatically as all roles were revoked.",
        event_type=EventType.USER_DEACTIVATED_NO_ROLES,
        event_context={"affected_user": user},
        scopes=[user],
    )


def reactivate_user_with_logging(user: User, reason: str = "Gained new role") -> None:
    """
    Reactivate a user and log the action.
    """
    user.is_active = True
    user.deactivation_reason = ""
    user.save(update_fields=["is_active", "deactivation_reason"])

    logger.info(
        f"User {user} (uuid={user.uuid}) has been reactivated automatically. Reason: {reason}"
    )

    event_logger.emit(
        f"User {{affected_user_username}} has been reactivated automatically. Reason: {reason}",
        event_type=EventType.USER_ACTIVATED,
        event_context={"affected_user": user},
        scopes=[user],
    )


def get_scope_name(scope):
    return getattr(scope, "name", str(scope))


def log(
    instance: UserRole,
    current_user: User | None,
    message: str,
    event_type: EventType,
    reason: str | None = None,
):
    model_name = instance.scope._meta.model_name
    role_name = instance.role.name
    customer = _get_customer(instance.scope)
    event_context = {
        "scope": instance.scope,
        "scope_type": model_name,
        "scope_uuid": instance.scope.uuid.hex,
        "scope_name": get_scope_name(instance.scope),
        "customer": customer,
        "affected_user": instance.user,
        "role_name": role_name,
    }
    if current_user:
        event_context["user"] = current_user
        event_context["initiated_by"] = (
            f"{current_user.full_name} ({current_user.username})"
        )
    else:
        event_context["initiated_by"] = "System"

    if reason:
        event_context["reason"] = reason

    event_logger.emit(
        message,
        event_type=event_type,
        event_context=event_context,
        scopes=[instance.scope, customer],
    )


def log_role_granted(
    sender,
    instance,
    current_user: User | None = None,
    reason: str | None = None,
    **kwargs,
):
    """Log the event of a user being granted a role."""
    if not reason:
        if current_user:
            reason = "Manual role assignment via API"
        else:
            reason = "System-initiated role assignment"

    message = (
        "User {affected_user_full_name} ({affected_user_username}) has gained role of {role_name} in {scope_name}. Initiated by: {initiated_by}. Reason: "
        + reason
    )

    log(
        instance,
        current_user,
        message=message,
        event_type=EventType.ROLE_GRANTED,
        reason=reason,
    )


def log_role_revoked(
    sender, instance, current_user=None, reason: str | None = None, **kwargs
):
    """Log the event of a user having a role revoked."""
    if not reason:
        if current_user:
            reason = "Manual role removal via API"
        elif (
            hasattr(instance, "expiration_time")
            and instance.expiration_time
            and instance.expiration_time <= timezone.now()
        ):
            reason = "Automatic expiration"
        else:
            reason = "System-initiated removal"

    message = (
        "User {affected_user_full_name} ({affected_user_username}) has lost role of {role_name} in {scope_name}. Initiated by: {initiated_by}. Reason: "
        + reason
    )

    log(
        instance,
        current_user,
        message=message,
        event_type=EventType.ROLE_REVOKED,
        reason=reason,
    )


def log_role_updated(
    sender, instance, current_user=None, reason: str | None = None, **kwargs
):
    """Log the event of a user's role being updated."""
    old_time = instance.tracker.previous("expiration_time")
    new_time = instance.expiration_time

    if not reason:
        if current_user:
            reason = "Manual role update via API"
        else:
            reason = "System-initiated role update"

    message = (
        "Permission expiration time for user {affected_user_full_name} ({affected_user_username}) "
        "in {scope_name} is updated from "
        f"{old_time} to {new_time}. Initiated by: {{initiated_by}}. Reason: {reason}"
    )

    log(
        instance,
        current_user,
        message=message,
        event_type=EventType.ROLE_UPDATED,
        reason=reason,
    )


def deactivate_user_if_no_roles(sender, instance, current_user=None, **kwargs):
    """Deactivate a user if they no longer have any active roles."""
    # Skip during import operations to avoid interfering with bulk data imports
    if get_skip_side_effects():
        return

    user = instance.user
    reason = get_deactivation_reason(user)
    if reason:
        deactivate_user_with_logging(user, reason)


def reactivate_user_if_gaining_roles(sender, instance, current_user=None, **kwargs):
    """Reactivate a user if they were previously deactivated and are now gaining roles."""
    # Skip during import operations to avoid interfering with bulk data imports
    if get_skip_side_effects():
        return

    user = instance.user
    if should_reactivate_user(user):
        reactivate_user_with_logging(user, "Gained a new role")


def revoke_user_roles_on_availability_removal(sender, instance, **kwargs):
    """Schedule async revocation when a RoleAvailability row is removed.

    The actual scan over ``UserRole`` rows for the affected role lives in
    :func:`waldur_core.permissions.tasks.reconcile_user_roles_for_role`;
    handlers should not iterate large querysets synchronously inside the
    request transaction.
    """
    # Function-local: tasks.py imports from handlers.py at top, so a
    # top-level import here would form a circular dependency.
    from waldur_core.permissions import tasks as permission_tasks

    role_id = instance.role_id
    if role_id is None:
        return
    transaction.on_commit(
        lambda: permission_tasks.reconcile_user_roles_for_role.delay(role_id)
    )
