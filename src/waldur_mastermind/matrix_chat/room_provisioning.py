"""Creation of Matrix rooms for Waldur scopes.

Provisioning is two-phase: this module writes the `MatrixRoom` row synchronously
in state CREATING, then hands the actual homeserver call to Celery once the
transaction commits. All three entry points — the API viewset, the
project-creation handler, and the `provision_matrix_rooms` backfill command —
go through here so the uniqueness and dispatch rules stay in one place.
"""

import logging

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Q

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import (
    check_pat_support_scope,
    get_scope_ids,
    has_permission_on_any_source,
)
from waldur_core.structure.models import Customer, Project

from . import models, tasks

logger = logging.getLogger(__name__)


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


def can_create_room(request, project):
    """The write check for one project, including a PAT's scope ceiling,
    which the queryset above cannot see."""
    if request.user.is_support and check_pat_support_scope(request):
        return True
    return has_permission_on_any_source(
        request, PermissionEnum.CREATE_MATRIX_ROOM, project, ["*", "customer"]
    )


def provision_project_room(project, created_by=None):
    """Create a MatrixRoom row for a project and schedule its provisioning.

    Returns the room, or None when a room already exists for the project.
    `MatrixRoom` is unique per (content_type, object_id), so a concurrent
    creation surfaces as IntegrityError rather than a second room; the loser of
    that race is treated the same as "already exists".
    """
    ct = ContentType.objects.get_for_model(project)
    # Project names allow twice the room name column; an overlong name would
    # otherwise raise inside project creation when auto-create is on.
    max_length = models.MatrixRoom._meta.get_field("room_name").max_length
    try:
        with transaction.atomic():
            room = models.MatrixRoom.objects.create(
                room_name=project.name[:max_length],
                content_type=ct,
                object_id=project.id,
                created_by=created_by,
            )
    except IntegrityError:
        logger.info(
            "Matrix room provisioning skipped: a room already exists for project %s",
            project.uuid,
        )
        return None

    room_uuid = str(room.uuid)
    transaction.on_commit(lambda: tasks.create_room.delay(room_uuid))
    return room
