from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Model

from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import (
    add_user,
    add_user_or_skip,
    delete_user,
    get_permissions,
    has_user,
)


class PermissionMixin:
    """
    Base permission management mixin for customer and project.
    It is expected that reverse `permissions` relation is created for this model.
    Provides method to grant, revoke and check object permissions.
    """

    def get_or_create_role(self, role=None):
        if isinstance(role, str):
            return Role.objects.get_system_role(
                name=role,
                content_type=ContentType.objects.get_for_model(self._meta.model),
            )
        return role

    def has_user(self, user, role=None, timestamp=False):
        role = self.get_or_create_role(role)
        return has_user(self, user, role, timestamp)

    @transaction.atomic()
    def add_user(self, user, role, created_by=None, expiration_time=None, force=False):
        role = self.get_or_create_role(role)
        permission = add_user(
            self, user, role, created_by, expiration_time, force=force
        )
        return permission

    @transaction.atomic()
    def add_user_or_skip(self, user, role, created_by=None, expiration_time=None):
        """Grant a role, skipping (with a log) if the org-scoping policy rejects
        it. For non-interactive callers where one rejection must not abort the
        whole operation (signal handlers, auto-provisioning, team-restore)."""
        role = self.get_or_create_role(role)
        return add_user_or_skip(
            self, user, role, created_by=created_by, expiration_time=expiration_time
        )

    @transaction.atomic()
    def remove_user(self, user, role=None, removed_by=None):
        role = self.get_or_create_role(role)
        if role:
            delete_user(self, user, role, removed_by, reason="User removed from scope")
        else:
            for perm in get_permissions(self, user):
                perm.revoke(removed_by, reason="All roles removed from scope")

    def get_users(self, role=None):
        """Return all connected to scope users"""
        raise NotImplementedError

    def get_user_mails(self, role=None):
        return (
            self.get_users(role)
            .exclude(email="")
            .exclude(notifications_enabled=False)
            .values_list("email", flat=True)
        )


def get_permission_aggregates() -> list[type[Model]]:
    return [model for model in apps.get_models() if issubclass(model, PermissionMixin)]
