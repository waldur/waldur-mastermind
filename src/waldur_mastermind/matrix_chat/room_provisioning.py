"""Creation of Matrix rooms for Waldur scopes.

Provisioning is two-phase: this module writes the `MatrixRoom` row synchronously
in state CREATING, then hands the actual homeserver call to Celery once the
transaction commits. All entry points go through here so the uniqueness and
dispatch rules stay in one place: for projects the API viewset, the
project-creation handler and the `provision_matrix_rooms` backfill command
(`provision_project_room`); for the objects other apps register in `scopes`,
those apps (`ensure_room`).
"""

import logging
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from waldur_core.core import access
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import (
    check_pat_support_scope,
    get_scope_ids,
    has_permission_on_any_source,
)
from waldur_core.structure.models import Customer, Project

from . import matrix_client, models, scopes, tasks

logger = logging.getLogger(__name__)

# How long a room may wait in CREATING before ensure_room takes its creation
# for lost (queued while chat was off, or a worker that died) and queues it
# again. create_room is not safe to run twice at once, so a room whose task
# may still be on its way is left to it.
CREATION_STALE_AFTER = timedelta(minutes=10)


class MatrixChatDisabled(RuntimeError):
    """Matrix chat is switched off, so no room can be created. A view turns
    it into a 400, as the room API's write guard does."""

    def __init__(self, message="Matrix chat is disabled."):
        super().__init__(message)


def creatable_projects(user):
    """Projects the user may create a room for, whether or not one exists yet.

    `eligible_projects` and the create endpoint's project lookup both use this,
    so the list a user sees and what they can submit cannot disagree.
    """
    projects = Project.available_objects.all()
    # Global support creates rooms by policy; has_permission only admits staff.
    if user.is_staff or user.is_support:
        return projects
    # Matched on the role's own permissions, as has_permission does: resolving
    # by role name would also admit a clone through its template's grant.
    permission = PermissionEnum.CREATE_MATRIX_ROOM
    customer_ct = ContentType.objects.get_for_model(Customer)
    project_ct = ContentType.objects.get_for_model(Project)
    return projects.filter(
        Q(customer_id__in=get_scope_ids(user, customer_ct, permission=permission))
        | Q(id__in=get_scope_ids(user, project_ct, permission=permission))
    )


# Who can_create_room lets create a room for the project a request names:
# MATRIX_ROOM.CREATE on the project or its organization, or global support.
# Change the two together.
CREATES_ROOM = access.any_of(
    access.permission(
        PermissionEnum.CREATE_MATRIX_ROOM, ["project", "project.customer"]
    ),
    access.staff_or_support(),
)


def can_create_room(request, project):
    """The write check for one project, including a PAT's scope ceiling,
    which the queryset above cannot see."""
    if request.user.is_support and check_pat_support_scope(request):
        return True
    return has_permission_on_any_source(
        request, PermissionEnum.CREATE_MATRIX_ROOM, project, ["*", "customer"]
    )


def _create_room(scope, name, created_by):
    """Write the scope's room in CREATING and create it on the homeserver
    after commit; None when the scope has a room already.

    `MatrixRoom` is unique per (content_type, object_id), so a concurrent
    creation surfaces as IntegrityError rather than a second room; the loser of
    that race is treated the same as "already exists".
    """
    # An overlong name would otherwise raise inside the caller's transaction,
    # such as project creation when auto-create is on.
    max_length = models.MatrixRoom._meta.get_field("room_name").max_length
    try:
        with transaction.atomic():
            room = models.MatrixRoom.objects.create(
                room_name=name[:max_length],
                content_type=ContentType.objects.get_for_model(scope),
                object_id=scope.pk,
                created_by=created_by,
            )
    except IntegrityError:
        return None

    room_uuid = str(room.uuid)
    transaction.on_commit(lambda: tasks.create_room.delay(room_uuid))
    return room


def restart_creation(room):
    """Queue the creation of a room, locked by the caller, again once the
    transaction commits; a room in error is moved back to CREATING first.
    Raises TransitionNotAllowed for a room in any other state."""
    if room.state != models.RoomStates.CREATING:
        room.retry_creating()
    # Saved also from CREATING: the new modified time stops ensure_room from
    # queueing it again while this dispatch is on its way.
    room.save(update_fields=["state", "error_message", "closing_export"])
    room_uuid = str(room.uuid)
    transaction.on_commit(lambda: tasks.create_room.delay(room_uuid))


def provision_project_room(project, created_by=None):
    """Create a MatrixRoom row for a project and schedule its provisioning.

    Returns the room, or None when a room already exists for the project.
    """
    room = _create_room(project, project.name, created_by)
    if room is None:
        logger.info(
            "Matrix room provisioning skipped: a room already exists for project %s",
            project.uuid,
        )
    return room


def ensure_room(scope, created_by=None, name=None):
    """The room of an object whose app registered its model in `scopes`,
    created if it has none yet. Returns (room, created).

    A room that exists is returned in whatever state it is: an archived room
    keeps its scope's place, as a project's does, and only staff reactivate it.
    One whose creation never finished is queued for creation again first: in
    error before it got a homeserver room, or in CREATING for longer than
    CREATION_STALE_AFTER. `name` defaults to the scope's name
    (scopes.scope_name).

    Raises MatrixChatDisabled while Matrix chat is switched off: a room
    written then would wait in CREATING for a task that does nothing.
    """
    if isinstance(scope, Project):
        raise ValueError("Project rooms are created with provision_project_room.")
    if not scopes.is_registered(scope):
        raise ValueError(
            f"{type(scope).__name__} is not registered as a Matrix room scope."
        )
    if not matrix_client.is_enabled():
        raise MatrixChatDisabled()
    lookup = {
        "content_type": ContentType.objects.get_for_model(scope),
        "object_id": scope.pk,
    }
    room = models.MatrixRoom.objects.filter(**lookup).first()
    if room is not None:
        if _creation_lost(room):
            room = _recreate(room)
        return room, False
    room = _create_room(
        scope, name or scopes.scope_name(scope) or str(scope), created_by
    )
    if room is None:
        # Created meanwhile by a transaction that has committed since.
        return models.MatrixRoom.objects.get(**lookup), False
    return room, True


def _creation_lost(room):
    if room.state == models.RoomStates.ERROR:
        # A room with a homeserver room erred later, disabling it say: staff
        # retry or disable it.
        return not room.room_id
    return (
        room.state == models.RoomStates.CREATING
        and room.modified < timezone.now() - CREATION_STALE_AFTER
    )


def _recreate(room):
    """Queue the creation of a room whose creation was lost again, unless a
    concurrent call has meanwhile."""
    with transaction.atomic():
        room = models.MatrixRoom.objects.select_for_update().get(pk=room.pk)
        if _creation_lost(room):
            logger.info(
                "Queueing the creation of Matrix room %s again from %s",
                room.uuid,
                room.state,
            )
            restart_creation(room)
    return room
