from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsAdminOrReadOnly(BasePermission):
    def has_permission(self, request, view):
        return (
            request.method in SAFE_METHODS or
            (request.user.is_authenticated and request.user.is_staff)
        )


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
            @decorators.detail_route(method='POST')
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
        if hasattr(view, view.action + '_permissions'):
            return getattr(view, view.action + '_permissions')
        # otherwise return view-level permissions + extra view permissions
        extra_permissions = getattr(view, view.action + 'extra_permissions', [])
        if request.method in SAFE_METHODS:
            return getattr(view, 'safe_methods_permissions', []) + extra_permissions
        else:
            return getattr(view, 'unsafe_methods_permissions', []) + extra_permissions

    def has_permission(self, request, view):
        for check in self.get_permission_checks(request, view):
            check(request, view)
        return True

    def has_object_permission(self, request, view, obj):
        for check in self.get_permission_checks(request, view):
            check(request, view, obj)
        return True
