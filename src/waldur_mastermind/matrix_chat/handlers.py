import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django_fsm import TransitionNotAllowed

from waldur_core.permissions.models import UserRole
from waldur_core.structure.models import Project
from waldur_mastermind.marketplace.enums import OrderStates

from . import matrix_client, tasks
from .models import MatrixRoom, RoomStates

logger = logging.getLogger(__name__)


def _get_room_for_project(project):
    """Get the active MatrixRoom for a project, or None if none exists."""
    ct = ContentType.objects.get_for_model(project)
    try:
        return MatrixRoom.objects.get(
            content_type=ct,
            object_id=project.id,
            state=RoomStates.ACTIVE,
        )
    except MatrixRoom.DoesNotExist:
        return None


def _notify_room(room, message):
    """Schedule a notification message to be sent to a room after the current transaction commits."""
    transaction.on_commit(
        lambda: tasks.send_room_notification.delay(str(room.uuid), message)
    )


def _format_role_name(role):
    """Format a role name for display, e.g. 'PROJECT.ADMIN' -> 'Project Admin'."""
    return role.name.replace(".", " ").title()


def on_role_granted(sender, instance: UserRole, **kwargs):
    """When a role is granted, invite the user to the project's Matrix room."""
    if not matrix_client.is_enabled():
        return

    scope = instance.scope
    if not isinstance(scope, Project):
        return

    room = _get_room_for_project(scope)
    if not room:
        return

    user = instance.user
    role_name = _format_role_name(instance.role)
    room_uuid = str(room.uuid)
    user_uuid = str(user.uuid)
    full_name = user.full_name or user.username

    def _on_commit():
        tasks.invite_user_to_room.delay(room_uuid, user_uuid)
        tasks.send_room_notification.delay(
            room_uuid,
            f"{full_name} has been granted the {role_name} role.",
        )

    transaction.on_commit(_on_commit)


def on_role_revoked(sender, instance: UserRole, **kwargs):
    """When a role is revoked, kick the user from the project's Matrix room if they have no remaining roles."""
    if not matrix_client.is_enabled():
        return

    scope = instance.scope
    if not isinstance(scope, Project):
        return

    room = _get_room_for_project(scope)
    if not room:
        return

    user = instance.user
    role_name = _format_role_name(instance.role)
    full_name = user.full_name or user.username

    # Always notify about the role change
    _notify_room(
        room,
        f"{full_name} has lost the {role_name} role.",
    )

    # Check if user has any remaining active roles in this project
    has_project_roles = UserRole.objects.filter(
        user=user,
        scope=scope,
        is_active=True,
    ).exists()

    # Check if user has customer-level roles (which also grant project access)
    has_customer_roles = UserRole.objects.filter(
        user=user,
        scope=scope.customer,
        is_active=True,
    ).exists()

    if has_project_roles or has_customer_roles:
        return

    room_uuid = str(room.uuid)
    user_uuid = str(user.uuid)

    transaction.on_commit(lambda: tasks.kick_user_from_room.delay(room_uuid, user_uuid))


def on_project_pre_delete(sender, instance, **kwargs):
    """When a project is about to be deleted, disable room (kick members, export, archive)."""
    if not matrix_client.is_enabled():
        return

    ct = ContentType.objects.get_for_model(instance)
    try:
        room = MatrixRoom.objects.get(
            content_type=ct,
            object_id=instance.id,
        )
    except MatrixRoom.DoesNotExist:
        return

    if room.state in (RoomStates.ACTIVE, RoomStates.ERROR):
        try:
            room.begin_disabling()
        except TransitionNotAllowed:
            # Another concurrent path already transitioned the room — leave it
            # alone rather than 500-ing inside the pre_delete signal handler.
            logger.info("Room %s skipped disable: already in %s", room.uuid, room.state)
            return
        room.save(update_fields=["state"])

    if room.state == RoomStates.DISABLING:
        room_uuid = str(room.uuid)
        transaction.on_commit(
            lambda: tasks.disable_room.delay(room_uuid, reason="project termination")
        )


def on_order_state_changed(sender, instance, created=False, **kwargs):
    """Notify the project's Matrix room when an order is approved, completed, or rejected."""
    if not matrix_client.is_enabled():
        return

    if created:
        return

    if not instance.tracker.has_changed("state"):
        return

    state = instance.state
    state_messages = {
        OrderStates.EXECUTING: "approved",
        OrderStates.DONE: "completed",
        OrderStates.REJECTED: "rejected",
        OrderStates.CANCELED: "canceled",
        OrderStates.ERRED: "failed",
    }

    verb = state_messages.get(state)
    if not verb:
        return

    project = instance.project
    if not project:
        return

    room = _get_room_for_project(project)
    if not room:
        return

    order_type = instance.get_type_display()
    offering_name = instance.offering.name if instance.offering else "unknown"
    created_by = instance.created_by
    user_name = (
        (created_by.full_name or created_by.username) if created_by else "System"
    )

    message = (
        f"Order {verb}: {order_type} of {offering_name} (requested by {user_name})."
    )

    _notify_room(room, message)
