from functools import reduce
from operator import or_

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from rest_framework import exceptions

from waldur_core.core.auth_utils import is_pat_auth
from waldur_core.core.models import User
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.pat_filtering import PATScopeListFilter
from waldur_core.permissions.utils import (
    check_pat_support_scope,
    get_scope_ids,
    has_permission_on_any_source,
)
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_core.structure.models import Customer, Project

from . import models


def get_accessible_room_ids(user):
    """Get MatrixRoom IDs accessible to the user based on project/customer roles."""
    project_ct = ContentType.objects.get_for_model(Project)
    customer_ct = ContentType.objects.get_for_model(Customer)

    connected_projects = get_connected_projects(user)
    connected_customers = get_connected_customers(user)

    # A customer role reaches the customer's project rooms on the same rule
    # as member sync, so the list holds no room the user would be kept out of.
    projects_via_customer = Project.objects.filter(
        customer__in=get_scope_ids(
            user, customer_ct, permission=PermissionEnum.CREATE_MATRIX_ROOM
        )
    ).values_list("id", flat=True)

    return models.MatrixRoom.objects.filter(
        Q(content_type=project_ct, object_id__in=connected_projects)
        | Q(content_type=project_ct, object_id__in=projects_via_customer)
        | Q(content_type=customer_ct, object_id__in=connected_customers)
    ).values_list("id", flat=True)


def get_manageable_room_ids(user):
    """MatrixRoom IDs the user manages through a role: the rooms they may
    create, on the project or on its customer.

    Removed projects are kept: the export made on termination is theirs.
    """
    permission = PermissionEnum.CREATE_MATRIX_ROOM
    project_ct = ContentType.objects.get_for_model(Project)
    customer_ct = ContentType.objects.get_for_model(Customer)
    customer_ids = get_scope_ids(user, customer_ct, permission=permission)
    projects = Project.objects.filter(
        Q(id__in=get_scope_ids(user, project_ct, permission=permission))
        | Q(customer_id__in=customer_ids)
    )
    return models.MatrixRoom.objects.filter(
        Q(content_type=project_ct, object_id__in=projects.values("id"))
        | Q(content_type=customer_ct, object_id__in=customer_ids)
    ).values_list("id", flat=True)


def get_unlinked_room_users():
    """Active users member sync puts in an active project room who have no
    Matrix profile.

    Provisioning refuses a user whose generated ID already has an account and
    leaves no profile or member row behind, so this is where they show up.
    """
    project_ct = ContentType.objects.get_for_model(Project)
    customer_ct = ContentType.objects.get_for_model(Customer)
    room_projects = models.MatrixRoom.objects.filter(
        state=models.RoomStates.ACTIVE, content_type=project_ct
    ).values("object_id")
    room_customers = Project.objects.filter(id__in=room_projects).values("customer_id")
    # The roles sync_project_members_to_room reads: any on the project, and
    # those on its customer that get_customer_roles_in_project_rooms keeps.
    roles = UserRole.objects.filter(is_active=True).filter(
        Q(content_type=project_ct, object_id__in=room_projects)
        | Q(
            content_type=customer_ct,
            object_id__in=room_customers,
            role__permissions__permission=PermissionEnum.CREATE_MATRIX_ROOM,
        )
    )
    return User.objects.filter(
        is_active=True,
        matrix_profile__isnull=True,
        id__in=roles.values("user_id"),
    ).order_by("username")


def can_manage_room(request, view, obj=None):
    """Whoever may create a room manages it day to day, syncing its members
    and exporting its history, and so does support. That is as far as either
    goes: the room's lifecycle is staff's.

    The write check for one room, including a PAT's scope ceiling, which the
    queryset above cannot see.
    """
    if not obj:
        return
    if request.user.is_support and check_pat_support_scope(request):
        return
    if has_permission_on_any_source(
        request, PermissionEnum.CREATE_MATRIX_ROOM, obj.scope, ["*", "customer"]
    ):
        return
    raise exceptions.PermissionDenied()


def pat_filter_exports(ids):
    """The exports a personal access token's bindings reach.

    Walks the room's scope up the way ``get_scope_ancestors`` does: a project
    binding reaches that project's room, a customer binding reaches the
    customer's room and those of all its projects, removed ones included.
    """
    project_ct = ContentType.objects.get_for_model(Project)
    customer_ct = ContentType.objects.get_for_model(Customer)
    project_ids = ids.get("project")
    customer_ids = ids.get("customer")
    lookups = []
    if project_ids:
        lookups.append(
            Q(room__content_type=project_ct, room__object_id__in=project_ids)
        )
    if customer_ids:
        lookups.append(
            Q(
                room__content_type=project_ct,
                room__object_id__in=Project.objects.filter(
                    customer_id__in=customer_ids
                ).values("id"),
            )
        )
        lookups.append(
            Q(room__content_type=customer_ct, room__object_id__in=customer_ids)
        )
    if not lookups:
        return None
    return reduce(or_, lookups)


def filter_exports_for_request(queryset, request):
    """Limit history exports to the ones the requester may download.

    An export is a room's whole history, media included, so it goes to whoever
    manages the room and no further. Room members and other role holders are
    left out: they read the room itself.

    Takes the request so that a personal access token is held to what it
    carries, as ``can_manage_room`` holds it on writes: its bindings, the
    support scope for support, and ``MATRIX_ROOM.CREATE`` for everyone else.
    A bare user is accepted too, and is checked on its roles alone.
    """
    user = getattr(request, "user", request)
    if not user.is_authenticated:
        return queryset.none()
    queryset = PATScopeListFilter().filter_queryset(request, queryset, None)
    if (user.is_staff or user.is_support) and check_pat_support_scope(request):
        return queryset
    auth = getattr(request, "auth", None)
    if is_pat_auth(auth) and PermissionEnum.CREATE_MATRIX_ROOM.value not in auth.scopes:
        return queryset.none()
    return queryset.filter(room__in=get_manageable_room_ids(user))
