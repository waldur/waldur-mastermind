from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from rest_framework import exceptions

from . import models


def has_permission(user, permission, scope):
    if user.is_staff:
        return True

    content_type = ContentType.objects.get_for_model(scope)
    roles = models.UserRole.objects.filter(
        user=user, is_active=True, object_id=scope.id, content_type=content_type
    ).values_list('role', flat=True)
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
            if has_permission(request.user, permission, scope):
                return
        else:
            for path in sources:
                source = scope
                if path != '*':
                    for part in path.split('.'):
                        source = getattr(source, part)
                if has_permission(request.user, permission, source):
                    return

        raise exceptions.PermissionDenied()

    return permission_function


def add_permission(role, permission):
    role, _ = models.Role.objects.get_or_create(
        name=role,
    )
    models.RolePermission.objects.create(
        role=role,
        permission=permission,
    )


def role_has_permission(role, permission):
    return models.RolePermission.objects.filter(
        role__name=role, permission=permission
    ).exists()


def get_users(role, scope):
    content_type = ContentType.objects.get_for_model(scope)
    user_ids = models.UserRole.objects.filter(
        is_active=True, object_id=scope.id, content_type=content_type, role__name=role
    ).values_list('user_id', flat=True)
    return get_user_model().objects.filter(id__in=user_ids)


def get_scope_ids(user, content_type, role=None):
    qs = models.UserRole.objects.filter(
        is_active=True, user=user, content_type=content_type
    )
    if role:
        qs = qs.filter(role__name=role)
    return qs.values_list('object_id', flat=True)
