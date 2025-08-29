from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.db.models.query import QuerySet
from rest_framework import exceptions

from waldur_core.core.models import User

from . import enums, models, signals


def has_permission(request, permission, scope):
    if isinstance(request, User):
        user = request
    else:
        user = request.user

    # Inactive users should not have any permissions
    if not user.is_active:
        return False

    if user.is_staff:
        return True

    # Handle None scope
    if scope is None:
        return False

    roles = models.UserRole.objects.filter(
        user=user, is_active=True, scope=scope
    ).values_list("role", flat=True)
    if not roles:
        return False
    return models.RolePermission.objects.filter(
        role__in=roles, permission=permission
    ).exists()


def permission_factory(permission, sources=None):
    def permission_function(request, view, scope=None):
        if not scope:
            return

        if not sources:
            if has_permission(request, permission, scope):
                return
        else:
            attribute_errors = 0
            for path in sources:
                try:
                    source = scope
                    if path != "*":
                        for part in path.split("."):
                            source = getattr(source, part)
                    if has_permission(request, permission, source):
                        return
                except AttributeError:
                    # Continue to next path if attribute doesn't exist
                    attribute_errors += 1
                    continue

            # If all paths failed due to AttributeError, raise AttributeError
            if attribute_errors == len(sources):
                raise AttributeError(
                    f"None of the attribute paths {sources} exist on the scope object"
                )

        raise exceptions.PermissionDenied()

    # Attach metadata to the function object
    # This makes the raw data available for inspection.
    setattr(permission_function, "permission", permission)
    setattr(permission_function, "sources", sources)

    return permission_function


def get_users(scope, role_name=None):
    users = models.UserRole.objects.filter(is_active=True, scope=scope)
    if role_name:
        users = users.filter(role__name=role_name)
    user_ids = users.values_list("user_id", flat=True)
    return User.objects.filter(id__in=user_ids)


def get_users_with_permission(scope, permission):
    user_ids = models.UserRole.objects.filter(
        is_active=True, scope=scope, role__permissions__permission=permission
    ).values_list("user_id", flat=True)
    return User.objects.filter(id__in=user_ids)


def get_scope_ids(user, content_type, role=None, permission=None):
    qs = models.UserRole.objects.filter(
        is_active=True, user=user, content_type=content_type
    )
    if role:
        if not isinstance(role, list | tuple):
            role = [role]
        qs = qs.filter(role__name__in=role)
    if permission:
        qs = qs.filter(role__permissions__permission=permission)
    return qs.order_by().values_list("object_id", flat=True).distinct()


def get_user_ids(content_type, scope_ids, role=None):
    if not isinstance(scope_ids, list | tuple | QuerySet):
        scope_ids = [scope_ids]
    qs = models.UserRole.objects.filter(
        is_active=True, object_id__in=scope_ids, content_type=content_type
    )
    if role:
        if isinstance(role, models.Role):
            qs = qs.filter(role=role)
        else:
            if not isinstance(role, list | tuple):
                role = [role]
            qs = qs.filter(role__name__in=role)
    return qs.values_list("user_id", flat=True)


def count_users(scope):
    return (
        models.UserRole.objects.filter(is_active=True, scope=scope)
        .values_list("user_id")
        .distinct()
        .count()
    )


def has_user(scope, user, role=None, expiration_time=False):
    """
    Checks whether user has role in entity.
    `expiration_time` can have the following values:
        - False (default) - check whether user has role in entity regardless of expiration.
        - None - check whether user has permanent role in entity.
        - Datetime object - check whether user will have role in entity at specific timestamp.
    """
    qs = models.UserRole.objects.filter(is_active=True, user=user, scope=scope)
    if role:
        qs = qs.filter(role=role)
    if expiration_time is None:
        qs = qs.filter(expiration_time=None)
    elif expiration_time is not False:
        # expiration_time is a datetime - check if role will be active at that time
        qs = qs.filter(
            Q(expiration_time=None) | Q(expiration_time__gte=expiration_time)
        )
    # When expiration_time is False, don't filter by expiration at all
    return qs.exists()


def get_permissions(scope, user=None):
    qs = models.UserRole.objects.filter(
        scope=scope, is_active=True, user__is_active=True
    )
    if user:
        qs = qs.filter(user=user)
    return qs


def add_user(scope, user, role, created_by=None, expiration_time=None):
    content_type = ContentType.objects.get_for_model(scope)
    permission = models.UserRole.objects.create(
        user=user,
        role=role,
        content_type=content_type,
        object_id=scope.id,
        expiration_time=expiration_time,
        created_by=created_by,
    )
    signals.role_granted.send(
        sender=models.UserRole,
        instance=permission,
        current_user=created_by,
    )
    return permission


def update_user(scope, user, role, expiration_time=None, current_user=None):
    try:
        permission = models.UserRole.objects.get(
            user=user,
            role=role,
            scope=scope,
            is_active=True,
        )
    except models.UserRole.DoesNotExist:
        return False
    permission.set_expiration_time(expiration_time, current_user)
    return permission


def delete_user(scope, user, role, current_user=None, reason=None):
    try:
        permission = models.UserRole.objects.get(
            user=user,
            role=role,
            scope=scope,
            is_active=True,
        )
    except models.UserRole.DoesNotExist:
        return False

    if not reason:
        if current_user:
            reason = "Manual user removal via API"
        else:
            reason = "System-initiated user removal"

    permission.revoke(current_user, reason=reason)
    return True


def get_customer(scope):
    model_name = scope._meta.model_name
    if model_name == "customer":
        return scope
    else:
        return scope.customer


def get_valid_content_types():
    return [
        ContentType.objects.get_by_natural_key(*pair)
        for pair in enums.TYPE_MAP.values()
    ]


def get_valid_models():
    return [ct.model_class() for ct in get_valid_content_types()]


def get_create_permission(model_class):
    return enums.CREATE_PERMISSIONS.get(model_class._meta.model_name)


def get_delete_permission(model_class):
    return enums.DELETE_PERMISSIONS.get(model_class._meta.model_name)


def get_update_permission(model_class):
    return enums.UPDATE_PERMISSIONS.get(model_class._meta.model_name)
