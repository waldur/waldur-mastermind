from rest_framework.permissions import SAFE_METHODS, BasePermission

from waldur_core.configuration import config
from waldur_core.core import access
from waldur_core.core.access import has_lookup, requires_object
from waldur_core.core.exceptions import IncompleteProfileException
from waldur_core.core.user_attributes import get_user_missing_mandatory_attributes
from waldur_core.permissions.utils import check_pat_staff_scope, check_pat_support_scope


class IsAdminOrReadOnly(BasePermission):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return (
            request.user.is_authenticated
            and request.user.is_staff
            and check_pat_staff_scope(request)
        )

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        if method in SAFE_METHODS:
            return access.read_access(requires_authentication)
        return [access.staff()]


class ActionsPermission(BasePermission):
    """
    Allow to define custom permission checks for all actions together and each action separately.

    It is possible to define permissions checks in next way:
     - view.safe_methods_permissions - list of checks for all safe methods (GET, OPTIONS, HEAD).
     - view.unsafe_methods_permissions - list of checks for all unsafe methods (POST, PUT, PATCH, DELETE).
     - view.<action>_extra_permissions - list of action extra permissions. Backend will check
                                         view level permissions and extra_permissions together.
     - view.<action>_permissions- list of all view permissions. Backend will not check view level
                                  permissions if action permissions are defined.

    Example. Define action level permissions:

        def is_staff(request, view, obj=None):
            if not request.user.is_staff:
                raise PermissionDenied('User has to be staff to perform this action.')

        class MyView(...):
            permission_classes = (ActionsPermission,)
            ...
            def action(...):
                ...

            action_permissions = [is_staff]  # action will be available only for staff

    Example. Define view level permissions and additional permissions for
    action:

        def is_staff(request, view, obj=None):
            if not request.user.is_staff:
                raise PermissionDenied('User has to be staff to perform this action.')

        def has_civil_number(request, view, obj=None):
            if not request.user.civil_number:
                raise PermissionDenied('User has to have civil number to perform this action.')

        class MyView(...):
            permission_classes = (ActionsPermission,)
            # only user with civil number will have access to all unsafe actions
            unsafe_methods_permissions = [has_civil_number]
            ...
            @decorators.action(detail=True, method='POST')
            def action(...):
                ...

            action_extra_permissions = [is_staff]  # only staff user with civil numbers will have access to action
    """

    def get_permission_checks(self, request, view):
        """
        Get permission checks that will be executed for current action.
        """
        if view.action is None:
            return []
        # if permissions are defined for view directly - use them.
        if hasattr(view, view.action + "_permissions"):
            return getattr(view, view.action + "_permissions")
        # otherwise return view-level permissions + extra view permissions
        extra_permissions = getattr(view, view.action + "_extra_permissions", [])
        if request.method in SAFE_METHODS:
            return getattr(view, "safe_methods_permissions", []) + extra_permissions
        else:
            return getattr(view, "unsafe_methods_permissions", []) + extra_permissions

    def has_permission(self, request, view):
        checks = self.get_permission_checks(request, view)

        # Checks with 'sources' need the object. Resolve it only when the URL
        # names one: that is what get_object() requires, and it also covers
        # routes wired by hand with as_view(), where DRF leaves view.detail None.
        if has_lookup(view) and any(requires_object(check) for check in checks):
            obj = view.get_object()
            for check in checks:
                if requires_object(check):
                    check(request, view, obj)
                else:
                    check(request, view)
            return True

        # On a collection route an object-scoped check has nothing to test and
        # passes; such routes must scope their queryset instead.
        # ActionsPermissionRoutesTest keeps them from being declared at all.
        for check in checks:
            check(request, view)
        return True

    def has_object_permission(self, request, view, obj):
        for check in self.get_permission_checks(request, view):
            check(request, view, obj)
        return True


class IsSupport(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_active
            and (request.user.is_staff or request.user.is_support)
            and check_pat_support_scope(request)
        )

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        return [access.staff_or_support()]


class IsSupportOrReadOnly(BasePermission):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return (
            request.user.is_authenticated
            and (request.user.is_staff or request.user.is_support)
            and check_pat_support_scope(request)
        )

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        if method in SAFE_METHODS:
            return access.read_access(requires_authentication)
        return [access.staff_or_support()]


class IsStaff(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.is_active
            and request.user.is_staff
            and check_pat_staff_scope(request)
        )

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        return [access.staff()]


class PATScopeAwareIsAdminUser(BasePermission):
    """Drop-in replacement for DRF's IsAdminUser that also enforces PAT scopes."""

    def has_permission(self, request, view):
        return request.user and request.user.is_staff and check_pat_staff_scope(request)

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        return [access.staff()]


class IsStaffWriterOrSupportReader(BasePermission):
    """Staff may read and write. Support may only read.

    Safe methods accept staff or support, including a personal access token
    with the support scope (the staff scope satisfies that check too).
    Other methods require staff and the staff personal-access-token scope.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if request.method in SAFE_METHODS:
            return (user.is_staff or user.is_support) and check_pat_support_scope(
                request
            )
        return user.is_staff and check_pat_staff_scope(request)

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        if method in SAFE_METHODS:
            return [access.staff_or_support()]
        return [access.staff()]


def has_staff_access(request) -> bool:
    """Whether the request may act as staff.

    A staff user's personal access token acts as staff only with the staff
    scope; scoped down to a few permissions it must not pass staff-only checks.
    """
    user = getattr(request, "user", None)
    return bool(user and user.is_staff and check_pat_staff_scope(request))


def has_support_access(request) -> bool:
    """Whether the request may act as staff or global support; a personal
    access token needs the staff or the support scope."""
    user = getattr(request, "user", None)
    return bool(
        user
        and (user.is_staff or getattr(user, "is_support", False))
        and check_pat_support_scope(request)
    )


class TokenCarriesUserRole(BasePermission):
    """Refuse a personal access token that would act with less than its
    user's staff or support role.

    For views that only see the user, so a token's narrower scopes cannot be
    applied there (the AI assistant picks its tools from ``user.is_staff``).
    The MCP server refuses the same tokens.
    """

    message = "This personal access token needs the staff or support scope here."

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        # Narrows how a request may be made, not who may make it.
        return []

    def has_permission(self, request, view):
        user = request.user
        if getattr(user, "is_staff", False) and not has_staff_access(request):
            return False
        if getattr(user, "is_support", False) and not has_support_access(request):
            return False
        return True


def can_read_constance_secrets(request):
    """Whether this request may receive unredacted Constance secret values.

    Staff session authentication qualifies. A personal access token qualifies
    only when it carries the staff scope, so a staff user holding a
    support-scoped token is treated as support.
    """
    user = getattr(request, "user", None)
    return bool(user and user.is_staff and check_pat_staff_scope(request))


class RequiresCompleteProfile(BasePermission):
    """
    Permission class that requires users to have a complete profile.

    When ENFORCE_MANDATORY_USER_ATTRIBUTES is True, users with missing
    mandatory attributes will be blocked from accessing the view.
    Staff users bypass this check.

    Note: This permission class is created for future use but is not
    applied to any ViewSets by default. To enable enforcement, add it
    to the permission_classes of specific ViewSets.
    """

    @classmethod
    def describe_access(cls, view, method, requires_authentication):
        # Narrows how a request may be made, not who may make it.
        return []

    def has_permission(self, request, view):
        if not config.ENFORCE_MANDATORY_USER_ATTRIBUTES:
            return True

        if not request.user.is_authenticated:
            return True

        if request.user.is_staff:
            return True

        missing = get_user_missing_mandatory_attributes(request.user)
        if missing:
            raise IncompleteProfileException(missing_fields=missing)

        return True
