from typing import cast

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core.managers import GenericKeyMixin
from waldur_core.core.mixins import ScopeMixin
from waldur_core.core.models import DescribableMixin, User, UuidMixin
from waldur_core.permissions.enums import RoleEnum

from . import signals
from .enums import TYPE_MAP


class RoleManager(models.Manager):
    def get_system_role(self, name: str, content_type) -> "Role":
        role, _ = self.get_or_create(
            name=name, defaults={"is_system_role": True, "content_type": content_type}
        )
        return role


class Role(DescribableMixin, UuidMixin):
    permissions: models.Manager["RolePermission"]

    name = models.CharField(unique=True, db_index=True, max_length=150)
    is_system_role = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    objects: RoleManager = RoleManager()
    content_type = models.ForeignKey(
        to=ContentType,
        on_delete=models.CASCADE,
        null=False,
        blank=False,
        related_name="+",
    )

    class Meta:
        ordering = ["name"]

    def add_permission(self, name):
        RolePermission.objects.get_or_create(role=self, permission=name)

    def delete_permission(self, name):
        RolePermission.objects.filter(role=self, permission=name).delete()

    @classmethod
    def project_roles(cls):
        return cls.objects.filter(is_active=True, name__startswith="PROJECT.")

    @classmethod
    def project_admin(cls):
        return cls.objects.get_system_role(
            RoleEnum.PROJECT_ADMIN,
            content_type=ContentType.objects.get_by_natural_key("structure", "project"),
        )

    def __str__(self):
        return f"{self.name}"


class UserRoleManager(GenericKeyMixin, models.Manager):
    pass


class UserRole(TimeStampedModel, ScopeMixin, UuidMixin):
    user = models.ForeignKey[User](
        on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL, db_index=True
    )
    role = models.ForeignKey(on_delete=models.CASCADE, to=Role, db_index=True)
    created_by = models.ForeignKey[User](
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
    )
    expiration_time = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(null=True, default=True, db_index=True)
    tracker = cast(
        FieldInstanceTracker, FieldTracker(fields=["expiration_time", "is_active"])
    )
    objects = UserRoleManager()

    def set_expiration_time(self, expiration_time, current_user=None):
        self.expiration_time = expiration_time
        self.save(update_fields=["expiration_time"])
        signals.role_updated.send(
            sender=self.__class__,
            instance=self,
            current_user=current_user,
        )

    def revoke(self, current_user=None):
        if not self.is_active:
            # user role is already revoked
            return
        self.is_active = False
        self.expiration_time = timezone.now()
        self.save(update_fields=["is_active", "expiration_time"])
        signals.role_revoked.send(
            sender=self.__class__,
            instance=self,
            current_user=current_user,
        )

    @classmethod
    def get_scope_model_references(cls):
        """Return the app.model references for supported scope types."""
        return [f"{app}.{model}" for _, (app, model) in TYPE_MAP.items()]

    @classmethod
    def get_scope_content_types(cls):
        """Get ContentType objects for all supported scopes."""
        from django.apps import apps

        model_refs = cls.get_scope_model_references()
        models = []

        for ref in model_refs:
            try:
                models.append(apps.get_model(ref))
            except LookupError:
                pass

        if models:
            return ContentType.objects.get_for_models(*models)
        return {}

    class Meta:
        ordering = ["created"]


class RolePermission(models.Model):
    role = models.ForeignKey(
        on_delete=models.CASCADE, to=Role, db_index=True, related_name="permissions"
    )
    permission = models.CharField(max_length=100, db_index=True)

    class Meta:
        unique_together = ("role", "permission")
