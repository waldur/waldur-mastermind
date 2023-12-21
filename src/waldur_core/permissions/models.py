from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from model_utils import FieldTracker
from model_utils.fields import AutoCreatedField

from waldur_core.core.managers import GenericKeyMixin
from waldur_core.core.mixins import ScopeMixin
from waldur_core.core.models import DescribableMixin, User, UuidMixin

from . import signals


class RoleManager(models.Manager):
    def get_system_role(self, name: str, content_type) -> 'Role':
        role, _ = self.get_or_create(
            name=name, defaults={'is_system_role': True, 'content_type': content_type}
        )
        return role


class Role(DescribableMixin, UuidMixin):
    name = models.CharField(unique=True, db_index=True, max_length=150)
    is_system_role = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    objects: RoleManager = RoleManager()
    content_type = models.ForeignKey(
        to=ContentType,
        on_delete=models.CASCADE,
        null=False,
        blank=False,
        related_name='+',
    )

    class Meta:
        ordering = ['name']

    def add_permission(self, name):
        RolePermission.objects.get_or_create(role=self, permission=name)

    def __str__(self):
        return f'{self.name}'


class UserRoleManager(GenericKeyMixin, models.Manager):
    pass


class UserRole(ScopeMixin, UuidMixin):
    user: User = models.ForeignKey(
        on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL, db_index=True
    )
    role = models.ForeignKey(on_delete=models.CASCADE, to=Role, db_index=True)
    created_by = models.ForeignKey(
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name='+',
    )
    created = AutoCreatedField()
    expiration_time = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(null=True, default=True, db_index=True)
    tracker = FieldTracker(fields=['expiration_time'])
    objects = UserRoleManager()

    def set_expiration_time(self, expiration_time, current_user=None):
        self.expiration_time = expiration_time
        self.save(update_fields=['expiration_time'])
        signals.role_updated.send(
            sender=self.__class__,
            instance=self,
            current_user=current_user,
        )

    def revoke(self, current_user=None):
        self.is_active = False
        self.expiration_time = timezone.now()
        self.save(update_fields=['is_active', 'expiration_time'])
        signals.role_revoked.send(
            sender=self.__class__,
            instance=self,
            current_user=current_user,
        )


class RolePermission(models.Model):
    role = models.ForeignKey(
        on_delete=models.CASCADE, to=Role, db_index=True, related_name='permissions'
    )
    permission = models.CharField(max_length=100, db_index=True)

    class Meta:
        unique_together = ('role', 'permission')
