from django.conf import settings
from django.db import models
from model_utils.fields import AutoCreatedField

from waldur_core.core.mixins import ScopeMixin
from waldur_core.core.models import DescribableMixin, NameMixin, UuidMixin


class Role(DescribableMixin, NameMixin, UuidMixin):
    pass


class UserRole(ScopeMixin, UuidMixin):
    user = models.ForeignKey(
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


class RolePermission(models.Model):
    role = models.ForeignKey(
        on_delete=models.CASCADE, to=Role, db_index=True, related_name='permissions'
    )
    permission = models.CharField(max_length=100, db_index=True)
