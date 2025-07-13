import logging

from constance import config

from waldur_core.core.models import User
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.models import UserRole
from waldur_core.structure.permissions import _get_customer

logger = logging.getLogger(__name__)


def get_scope_name(scope):
    return getattr(scope, "name", str(scope))


def log(
    instance: UserRole, current_user: User | None, message: str, event_type: EventType
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
    event_logger.emit(
        message,
        event_type=event_type,
        event_context=event_context,
        scopes=[instance.scope, customer],
    )


def log_role_granted(sender, instance, current_user: User | None = None, **kwargs):
    """Log the event of a user being granted a role."""
    log(
        instance,
        current_user,
        message="User {affected_user_full_name} ({affected_user_username}) has gained role of {role_name} in {scope_name}.",
        event_type=EventType.ROLE_GRANTED,
    )


def log_role_revoked(sender, instance, current_user=None, **kwargs):
    """Log the event of a user having a role revoked."""
    log(
        instance,
        current_user,
        message="User {affected_user_full_name} ({affected_user_username}) has lost role of {role_name} in {scope_name}.",
        event_type=EventType.ROLE_REVOKED,
    )


def log_role_updated(sender, instance, current_user=None, **kwargs):
    """Log the event of a user's role being updated."""
    old_time = (instance.tracker.previous("expiration_time"),)
    new_time = (instance.expiration_time,)
    log(
        instance,
        current_user,
        message="Permission expiration time for user {affected_user_full_name} ({affected_user_username}) "
        "in {scope_name} is updated from "
        f"{old_time} to {new_time}.",
        event_type=EventType.ROLE_UPDATED,
    )


def deactivate_user_if_no_roles(sender, instance, current_user=None, **kwargs):
    """Deactivate a user if they no longer have any active roles."""
    if not config.DEACTIVATE_USER_IF_NO_ROLES:
        return
    user = instance.user
    has_active_roles = UserRole.objects.filter(user=user, is_active=True).exists()
    if (
        not has_active_roles
        and user.is_active
        and not user.is_staff
        and not user.is_support
    ):
        user.is_active = False
        user.save(update_fields=["is_active"])

        logger.info(
            f"User {user} (uuid={user.uuid}) has been deactivated automatically as all roles were revoked."
        )

        event_logger.emit(
            "User {affected_user_username} has been deactivated automatically as all roles were revoked.",
            event_type=EventType.USER_DEACTIVATED_NO_ROLES,
            event_context={"affected_user": user},
            scopes=[user],
        )
