import logging

from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django_fsm import TransitionNotAllowed

from waldur_core.permissions.models import UserRole
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.marketplace.enums import OrderStates

from . import matrix_client, room_provisioning, tasks
from .models import (
    MatrixRoom,
    MatrixRoomMember,
    MatrixUserProfile,
    MembershipStates,
    RoomStates,
    get_customer_roles_in_project_rooms,
    keeps_room_access,
)

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
    """When a role is granted, invite the user to the rooms it gives access to."""
    if not matrix_client.is_enabled():
        return

    scope = instance.scope
    if isinstance(scope, Customer):
        # The same rule as member sync, which only runs on demand; without
        # this the user stays out of each room until someone syncs it.
        if (
            not get_customer_roles_in_project_rooms(scope)
            .filter(pk=instance.pk)
            .exists()
        ):
            return
        rooms = MatrixRoom.objects.filter(
            content_type=ContentType.objects.get_for_model(Project),
            object_id__in=Project.objects.filter(customer=scope).values("id"),
            state=RoomStates.ACTIVE,
        )
        room_uuids = [str(room.uuid) for room in rooms]
        user_uuid = str(instance.user.uuid)

        def _invite():
            for room_uuid in room_uuids:
                tasks.invite_user_to_room.delay(room_uuid, user_uuid)

        transaction.on_commit(_invite)
        return
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
    """When a role is revoked, kick the user from the rooms it gave them access to."""
    if not matrix_client.is_enabled():
        return

    scope = instance.scope
    user = instance.user
    if isinstance(scope, Project):
        room = _get_room_for_project(scope)
        if not room:
            return
        role_name = _format_role_name(instance.role)
        full_name = user.full_name or user.username
        _notify_room(room, f"{full_name} has lost the {role_name} role.")
        rooms = [room]
    elif isinstance(scope, Customer):
        # Member sync invites customer-level role holders into every room of
        # the customer's projects, so losing that role must take them out.
        # Only rooms the user is in: a kick for any other room fails and retries.
        # A subquery, so both conditions apply to the user's own membership.
        in_rooms = (
            MatrixRoomMember.objects.filter(user=user)
            .exclude(membership_state__in=MembershipStates.GONE)
            .values("room_id")
        )
        rooms = MatrixRoom.objects.filter(
            content_type=ContentType.objects.get_for_model(Project),
            object_id__in=Project.objects.filter(customer=scope).values("id"),
            state=RoomStates.ACTIVE,
            id__in=in_rooms,
        )
    else:
        return

    for room in rooms:
        if keeps_room_access(user, room):
            continue
        room_uuid = str(room.uuid)
        user_uuid = str(user.uuid)
        transaction.on_commit(
            lambda room_uuid=room_uuid, user_uuid=user_uuid: (
                tasks.kick_user_from_room.delay(room_uuid, user_uuid)
            )
        )


def on_user_deactivated(sender, instance, created=False, **kwargs):
    """Sign out every Matrix device of a deactivated user and remove them from their rooms."""
    # previous() rather than has_changed(): a receiver that re-saves a new user
    # inside its own post_save reaches this one before the tracker is reset,
    # so has_changed() is true for a user who was never active.
    if created or instance.is_active or not instance.tracker.previous("is_active"):
        return
    # Not is_enabled(): open drawers keep refreshing with the homeserver after
    # chat is switched off, so their devices still have to be signed out.
    if not matrix_client.is_homeserver_configured():
        return

    user_uuid = instance.uuid.hex
    transaction.on_commit(lambda: tasks.end_matrix_access.delay(user_uuid))


def on_user_demoted(sender, instance, created=False, **kwargs):
    """Take former staff and support out of the rooms they joined with the
    Join button, which exempts them from member sync only while they are."""
    if created or instance.is_staff or instance.is_support:
        return
    tracker = instance.tracker
    if not (tracker.previous("is_staff") or tracker.previous("is_support")):
        return
    if not matrix_client.is_enabled():
        return

    room_uuids = [
        str(uuid)
        for uuid in MatrixRoomMember.objects.filter(user=instance, manually_joined=True)
        .exclude(membership_state__in=MembershipStates.GONE)
        .values_list("room__uuid", flat=True)
    ]
    user_uuid = str(instance.uuid)

    def _on_commit():
        # The task keeps anyone a role still lets in.
        for room_uuid in room_uuids:
            tasks.kick_user_from_room.delay(room_uuid, user_uuid)

    transaction.on_commit(_on_commit)


def on_user_reactivated(sender, instance, created=False, **kwargs):
    """Bring a reactivated user back into the rooms deactivation removed them from."""
    if created or not instance.is_active or instance.tracker.previous("is_active"):
        return
    # None: a user just created inside another receiver's post_save.
    if instance.tracker.previous("is_active") is None:
        return
    if not matrix_client.is_enabled():
        return

    project_ids = UserRole.objects.filter(
        user=instance,
        is_active=True,
        content_type=ContentType.objects.get_for_model(Project),
    ).values("object_id")
    customer_ids = UserRole.objects.filter(
        user=instance,
        is_active=True,
        content_type=ContentType.objects.get_for_model(Customer),
    ).values("object_id")
    projects = Project.objects.filter(id__in=project_ids) | Project.objects.filter(
        customer_id__in=customer_ids
    )
    room_uuids = [
        str(uuid)
        for uuid in MatrixRoom.objects.filter(
            content_type=ContentType.objects.get_for_model(Project),
            object_id__in=projects.values("id"),
            state=RoomStates.ACTIVE,
        ).values_list("uuid", flat=True)
    ]

    def _on_commit():
        for room_uuid in room_uuids:
            tasks.sync_project_members_to_room.delay(room_uuid)

    transaction.on_commit(_on_commit)


def on_user_pre_delete(sender, instance, **kwargs):
    """End a deleted user's Matrix sessions and room memberships.

    The Matrix profile and room memberships are deleted with the user, so they
    are read now and handed to the task, which runs once the deletion commits.
    """
    profile = MatrixUserProfile.objects.filter(user=instance).first()
    if profile is None:
        return
    matrix_user_id = profile.matrix_user_id
    # As on deactivation, switching chat off does not end open drawers. Once
    # the profile is gone, nothing could find these devices again.
    if not matrix_client.is_homeserver_configured():
        logger.warning(
            "Cannot end Matrix access of deleted %s: no homeserver", matrix_user_id
        )
        return
    room_ids = list(
        MatrixRoomMember.objects.filter(user=instance, room__room_id__gt="")
        .exclude(membership_state__in=MembershipStates.GONE)
        .values_list("room__room_id", flat=True)
    )
    transaction.on_commit(
        lambda: tasks.end_deleted_user_access.delay(matrix_user_id, room_ids)
    )


def on_project_created(sender, instance, created=False, raw=False, **kwargs):
    """Provision a Matrix room for a newly created project, when opted in.

    Off by default: it is gated on MATRIX_AUTO_CREATE_PROJECT_ROOMS on top of
    the usual MATRIX_ENABLED check, so enabling Matrix chat does not silently
    start creating a room per project on an existing deployment.
    """
    if not created or raw:
        return

    if not matrix_client.is_enabled():
        return

    if not config.MATRIX_AUTO_CREATE_PROJECT_ROOMS:
        return

    # Project has no created_by field, so the room is left unattributed —
    # same as a room provisioned by the backfill command.
    room = room_provisioning.provision_project_room(instance)
    if room:
        logger.info(
            "Auto-creating Matrix room %s for new project %s",
            room.uuid,
            instance.uuid,
        )


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


def on_history_export_deleted(sender, instance, **kwargs):
    # The files are rows in the media table that only the export points to, so
    # they would outlive it as full copies of the history nobody can reach.
    for field in (instance.export_file, instance.media_file):
        if field:
            field.delete(save=False)
