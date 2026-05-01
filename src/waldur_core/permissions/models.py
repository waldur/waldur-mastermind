from typing import cast

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from model_utils import FieldTracker
from model_utils.models import TimeStampedModel
from model_utils.tracker import FieldInstanceTracker

from waldur_core.core.managers import GenericKeyMixin
from waldur_core.core.mixins import ScopeMixin
from waldur_core.core.models import DescribableMixin, User, UuidMixin

from . import signals


class RoleManager(models.Manager):
    _cache: dict[str, "Role"] = {}

    def get_system_role(self, name: str, content_type) -> "Role":
        cache_key = name.value if hasattr(name, "value") else name
        if cache_key in self._cache:
            return self._cache[cache_key]
        role, _ = self.get_or_create(
            name=cache_key,
            defaults={"is_system_role": True, "content_type": content_type},
        )
        self._cache[cache_key] = role
        return role

    @classmethod
    def clear_cache(cls):
        cls._cache.clear()


class Role(DescribableMixin, UuidMixin):
    permissions: models.Manager["RolePermission"]

    name = models.CharField(db_index=True, max_length=150)
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

    def save(self, *args, **kwargs):
        # Enforce name+content_type uniqueness for non-resource scopes.
        # Resource/ResourceProject roles can have duplicate names
        # because different offerings define their own roles.
        if self.content_type_id:
            model_name = self.content_type.model
            if model_name not in ("resource", "resourceproject"):
                qs = Role.objects.filter(name=self.name, content_type=self.content_type)
                if self.pk:
                    qs = qs.exclude(pk=self.pk)
                if qs.exists():
                    raise ValidationError(
                        {
                            "name": "A role with this name already exists for this scope type."
                        }
                    )
        super().save(*args, **kwargs)

    def add_permission(self, name):
        RolePermission.objects.get_or_create(role=self, permission=name)

    def delete_permission(self, name):
        RolePermission.objects.filter(role=self, permission=name).delete()

    def __str__(self):
        return f"{self.name} ({self.is_active})"


class RoleAvailability(UuidMixin):
    """Controls where a Role can be assigned.

    When no RoleAvailability records exist for a role, it is available
    everywhere (system role behavior). When records exist, the role is
    only usable in those scopes (typically a specific Offering, Customer
    or similar).

    Logical "kind" grouping (e.g. all offerings of a Rancher-cluster
    profile) is expressed via :class:`marketplace.OfferingProfile` —
    binding an offering to a profile creates one RoleAvailability per
    catalog role on that offering, reconciled via Celery tasks.
    """

    role = models.ForeignKey(
        Role, on_delete=models.CASCADE, related_name="availability"
    )
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="+"
    )
    object_id = models.PositiveIntegerField()
    scope = GenericForeignKey("content_type", "object_id")

    class Meta:
        unique_together = ("role", "content_type", "object_id")


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

    def set_expiration_time(self, expiration_time, current_user=None, reason=None):
        self.expiration_time = expiration_time
        self.save(update_fields=["expiration_time"])
        if not reason:
            if current_user:
                reason = "Manual role update via API"
            else:
                reason = "System-initiated role update"
        signals.role_updated.send(
            sender=self.__class__,
            instance=self,
            current_user=current_user,
            reason=reason,
        )

    def revoke(self, current_user=None, reason=None):
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
            reason=reason,
        )

    class Meta:
        ordering = ["created"]

    def __str__(self):
        return f"{self.user.username} ({self.role}, {self.expiration_time}, {self.is_active})"


class RolePermission(models.Model):
    role = models.ForeignKey(
        on_delete=models.CASCADE, to=Role, db_index=True, related_name="permissions"
    )
    permission = models.CharField(max_length=100, db_index=True)

    class Meta:
        unique_together = ("role", "permission")
