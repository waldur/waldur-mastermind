import logging
from functools import reduce

from rest_framework import exceptions, permissions

from waldur_core.core import access
from waldur_core.core.permissions import (
    SAFE_METHODS,
    IsAdminOrReadOnly,
    has_staff_access,
    has_support_access,
)
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.utils import (
    check_pat_support_scope,
    has_permission,
    has_permission_on_any_source,
)
from waldur_core.structure import models

logger = logging.getLogger(__name__)


class IsStaffOrSupportUser(permissions.BasePermission):
    """
    Allows access only to staff or global support users.
    """

    def has_permission(self, request, view):
        return (
            request.user.is_staff or request.user.is_support
        ) and check_pat_support_scope(request)

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        return [access.staff_or_support()]


# TODO: this is a temporary permission filter.
class IsAdminOrOwner(IsAdminOrReadOnly):
    """
    Allows access to admin users or account's owner for modifications.
    For other users read-only access.
    """

    def has_permission(self, request, view):
        user = request.user
        if has_staff_access(request) or request.method in SAFE_METHODS:
            return True
        elif view.suffix == "List" or request.method == "DELETE":
            return False
        # Fix for schema generation
        elif "uuid" not in view.kwargs:
            return False

        return user == view.get_object()

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        if method in SAFE_METHODS:
            return access.read_access(requires_authentication)
        if method == "DELETE" or not getattr(view, "detail", False):
            return [access.staff()]
        return [
            access.any_of(
                access.staff(), access.owned("The user account the request is for.")
            )
        ]


class _RequestCheck(access.Check):
    """Pass a request that ``test(request)`` accepts."""

    def __init__(self, name, test, entry):
        self.name = name
        self.test = test
        self.entry = entry

    def __call__(self, request, view, obj=None, **kwargs):
        if not self.test(request):
            raise exceptions.PermissionDenied()

    def describe(self):
        return dict(self.entry)


is_staff = _RequestCheck("is_staff", has_staff_access, access.staff())
is_staff_or_support = _RequestCheck(
    "is_staff_or_support", has_support_access, access.staff_or_support()
)


# Who acts for a customer, project or offering, as the roles that pass. Each
# list extends the one before it, and a request acting as staff passes all of
# them. The checks below enforce these lists and describe themselves from
# them in the API schema, so the two cannot disagree.
OWNER_ROLES = ((RoleEnum.CUSTOMER_OWNER, "customer"),)
PROJECT_MANAGER_ROLES = OWNER_ROLES + ((RoleEnum.PROJECT_MANAGER, "project"),)
PROJECT_ADMIN_ROLES = PROJECT_MANAGER_ROLES + ((RoleEnum.PROJECT_ADMIN, "project"),)
SERVICE_MANAGER_ROLES = OWNER_ROLES + ((RoleEnum.CUSTOMER_MANAGER, "customer"),)
OFFERING_MANAGER_ROLES = SERVICE_MANAGER_ROLES + (
    (RoleEnum.OFFERING_MANAGER, "offering"),
)

# The role rows, looked up only when a check runs: describing a check must
# not touch the database, since the schema is generated without one.
_ROLE_ROWS = {
    RoleEnum.CUSTOMER_OWNER: lambda: CustomerRole.OWNER,
    RoleEnum.PROJECT_MANAGER: lambda: ProjectRole.MANAGER,
    RoleEnum.PROJECT_ADMIN: lambda: ProjectRole.ADMIN,
    RoleEnum.CUSTOMER_MANAGER: lambda: ServiceProviderRole.MANAGER,
    RoleEnum.OFFERING_MANAGER: lambda: OfferingRole.MANAGER,
}


def _holds_listed_role(user, holders, scopes) -> bool:
    return any(
        scopes.get(scope) is not None
        and scopes[scope].has_user(user, _ROLE_ROWS[role]())
        for role, scope in holders
    )


def _customer_scopes(obj, **kwargs):
    return {"customer": _get_customer(obj, **kwargs)}


def _project_scopes(obj, **kwargs):
    project = _get_project(obj, **kwargs)
    return {"customer": project.customer, "project": project}


def _offering_scopes(obj, **kwargs):
    return {"customer": _get_customer(obj, **kwargs), "offering": obj}


class _ObjectRoleCheck(access.Check):
    """Pass staff, or a holder of a listed role on the object's customer,
    project or offering. Without an object there is nothing to test."""

    needs_object = True

    def __init__(self, name, holders, scopes):
        self.name = name
        self.holders = holders
        self.scopes = scopes

    def __call__(self, request, view, obj=None, **kwargs):
        if not obj:
            return
        scopes = self.scopes(obj, **kwargs)
        if not (
            has_staff_access(request)
            or _holds_listed_role(request.user, self.holders, scopes)
        ):
            raise exceptions.PermissionDenied()

    def describe(self):
        return access.role(self.holders)


is_owner = _ObjectRoleCheck("is_owner", OWNER_ROLES, _customer_scopes)
is_administrator = _ObjectRoleCheck(
    "is_administrator", PROJECT_ADMIN_ROLES, _project_scopes
)
is_service_manager = _ObjectRoleCheck(
    "is_service_manager", SERVICE_MANAGER_ROLES, _customer_scopes
)
is_offering_manager = _ObjectRoleCheck(
    "is_offering_manager", OFFERING_MANAGER_ROLES, _offering_scopes
)


# `staff` is whether the user acts as staff; permission checks pass
# `has_staff_access(request)`, so that a personal access token without the
# staff scope does not. It defaults to the user's flag.


def _has_role_access(user, holders, scopes, staff=None) -> bool:
    if staff is None:
        staff = user.is_staff
    return staff or _holds_listed_role(user, holders, scopes)


def _has_owner_access(user, customer: models.Customer, staff=None):
    return _has_role_access(user, OWNER_ROLES, {"customer": customer}, staff)


def _has_manager_access(user, project: models.Project, staff=None):
    scopes = {"customer": project.customer, "project": project}
    return _has_role_access(user, PROJECT_MANAGER_ROLES, scopes, staff)


def _has_service_manager_access(user, customer: models.Customer, staff=None):
    return _has_role_access(user, SERVICE_MANAGER_ROLES, {"customer": customer}, staff)


def _has_admin_access(user, project: models.Project, staff=None):
    scopes = {"customer": project.customer, "project": project}
    return _has_role_access(user, PROJECT_ADMIN_ROLES, scopes, staff)


def _holds_on_project(request, permission, project: models.Project):
    return has_permission_on_any_source(request, permission, project, ["*", "customer"])


def can_answer_project_metadata(request, view, obj=None, **kwargs):
    """Check if user may answer the project's metadata checklist.

    Owners and project managers may by their role, and so may anyone holding
    PROJECT.UPDATE_METADATA on the project or on its organization.
    """
    if not obj:
        return
    project = _get_project(obj, **kwargs)
    if not (
        _has_manager_access(request.user, project, has_staff_access(request))
        or _holds_on_project(request, PermissionEnum.UPDATE_PROJECT_METADATA, project)
    ):
        raise exceptions.PermissionDenied()


def has_project_access(request, view, obj=None, **kwargs):
    """Check if user may read the project's metadata checklist.

    That is anyone with a project role, an owner, global support, anyone who
    may answer it, and anyone who may update the project, which shows them the
    checklist on its settings page.
    """
    if not obj:
        return
    project = _get_project(obj, **kwargs)
    if not (
        _has_admin_access(
            request.user, project, has_staff_access(request)
        )  # Covers customer owner + project admin/manager
        or project.has_user(request.user, ProjectRole.MEMBER)  # Project members
        or _holds_on_project(request, PermissionEnum.UPDATE_PROJECT_METADATA, project)
        or _holds_on_project(request, PermissionEnum.UPDATE_PROJECT, project)
        or has_support_access(request)
    ):
        raise exceptions.PermissionDenied()


def _get_parent_by_permission_path(obj, permission_path):
    path = getattr(obj.Permissions, permission_path, None)
    if path is None:
        return
    if path == "self":
        return obj

    return reduce(getattr, path.split("__"), obj)


def _get_project(obj, **kwargs) -> models.Project:
    return _get_parent_by_permission_path(obj, "project_path", **kwargs)


def _get_customer(obj, **kwargs) -> models.Customer:
    return _get_parent_by_permission_path(obj, "customer_path", **kwargs)


def can_move_project(request, view, obj=None):
    """
    Check if user can move a project.
    Staff users can always move projects, unless they act through a token
    without staff access: that token's scopes and bindings decide, as for
    anyone else's.
    Regular users can move projects if they have CREATE_PROJECT permission
    in BOTH the source and target organizations.
    """
    if has_staff_access(request):
        return

    if not obj:
        # We can't use view.get_object() here because it applies permission filtering.
        # Instead, get the object directly.
        try:
            uuid = view.kwargs.get("uuid")
            if not uuid:
                raise exceptions.PermissionDenied()
            obj = models.Project.objects.get(uuid=uuid)
        except models.Project.DoesNotExist:
            raise exceptions.PermissionDenied()

    source_customer = obj.customer

    serializer = view.get_serializer(data=request.data)
    if not serializer.is_valid():
        raise exceptions.PermissionDenied()

    target_customer = serializer.validated_data.get("customer")

    if not target_customer:
        raise exceptions.PermissionDenied()

    # Check if user has CREATE_PROJECT permission in both organizations
    has_source_permission = has_permission(
        request, PermissionEnum.CREATE_PROJECT, source_customer
    )
    has_target_permission = has_permission(
        request, PermissionEnum.CREATE_PROJECT, target_customer
    )

    if not (has_source_permission and has_target_permission):
        raise exceptions.PermissionDenied()
