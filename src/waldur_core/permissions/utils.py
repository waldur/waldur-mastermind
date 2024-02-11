from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.db.models.query import QuerySet
from rest_framework import exceptions

from . import enums, models, signals

User = get_user_model()


def has_permission(request, permission, scope):
    if isinstance(request, User):
        user = request
    else:
        user = request.user

    if user.is_staff:
        return True

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
            for path in sources:
                source = scope
                if path != "*":
                    for part in path.split("."):
                        source = getattr(source, part)
                if has_permission(request, permission, source):
                    return

        raise exceptions.PermissionDenied()

    return permission_function


def role_has_permission(role, permission):
    return models.RolePermission.objects.filter(
        role__name=role, permission=permission
    ).exists()


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
    return qs.values_list("object_id", flat=True).distinct()


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


def has_user(scope, user, role=None, expiration_time=None):
    """
    Checks whether user has role in entity.
    `expiration_time` can have following values:
        - False - check whether user has role in entity at the moment.
        - None - check whether user has permanent role in entity.
        - Datetime object - check whether user will have role in entity at specific timestamp.
    """
    qs = models.UserRole.objects.filter(is_active=True, user=user, scope=scope)
    if role:
        qs = qs.filter(role=role)
    if expiration_time is None:
        qs = qs.filter(expiration_time=None)
    elif expiration_time:
        qs = qs.filter(
            Q(expiration_time=None) | Q(expiration_time__gte=expiration_time)
        )
    return qs.exists()


def get_permissions(scope, user=None):
    qs = models.UserRole.objects.filter(scope=scope, is_active=True)
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


def delete_user(scope, user, role, current_user=None):
    try:
        permission = models.UserRole.objects.get(
            user=user,
            role=role,
            scope=scope,
            is_active=True,
        )
    except models.UserRole.DoesNotExist:
        return False
    permission.revoke(current_user)
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


def get_creation_permission(model_class):
    return enums.PERMISSIONS_MAP.get(model_class._meta.model_name)
