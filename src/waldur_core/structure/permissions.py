from functools import reduce
import logging

from django.conf import settings as django_settings
from rest_framework import exceptions

from waldur_core.core.permissions import SAFE_METHODS, IsAdminOrReadOnly
from waldur_core.structure import models

logger = logging.getLogger(__name__)


# TODO: this is a temporary permission filter.
class IsAdminOrOwner(IsAdminOrReadOnly):
    """
    Allows access to admin users or account's owner for modifications.
    For other users read-only access.
    """

    def has_permission(self, request, view):
        user = request.user
        if user.is_staff or request.method in SAFE_METHODS:
            return True
        elif view.suffix == 'List' or request.method == 'DELETE':
            return False
        # Fix for schema generation
        elif 'uuid' not in view.kwargs:
            return False

        return user == view.get_object()


def is_staff(request, view, obj=None):
    if not request.user.is_staff:
        raise exceptions.PermissionDenied()


def is_owner(request, view, obj=None):
    if not obj:
        return
    customer = _get_customer(obj)
    if not _has_owner_access(request.user, customer):
        raise exceptions.PermissionDenied()


def is_manager(request, view, obj=None):
    if not obj:
        return
    project = _get_project(obj)
    if not _has_manager_access(request.user, project):
        raise exceptions.PermissionDenied()


def is_administrator(request, view, obj=None):
    if not obj:
        return
    project = _get_project(obj)
    if not _has_admin_access(request.user, project):
        raise exceptions.PermissionDenied()


def _has_owner_access(user, customer):
    return user.is_staff or customer.has_user(user, models.CustomerRole.OWNER)


def _has_manager_access(user, project):
    return _has_owner_access(user, project.customer) or project.has_user(user, models.ProjectRole.MANAGER)


def _has_admin_access(user, project):
    return _has_manager_access(user, project) or project.has_user(user, models.ProjectRole.ADMINISTRATOR)


def _get_parent_by_permission_path(obj, permission_path):
    path = getattr(obj.Permissions, permission_path, None)
    if path is None:
        return
    if path == 'self':
        return obj
    return reduce(getattr, path.split('__'), obj)


def _get_project(obj):
    return _get_parent_by_permission_path(obj, 'project_path')


def _get_customer(obj):
    return _get_parent_by_permission_path(obj, 'customer_path')


def check_access_to_services_management(request, view, obj=None):
    if django_settings.WALDUR_CORE['ONLY_STAFF_MANAGES_SERVICES'] and not request.user.is_staff:
        raise exceptions.PermissionDenied()
