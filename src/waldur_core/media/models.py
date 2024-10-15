from django.db import models
from model_utils.models import TimeStampedModel

from waldur_core.core.models import UuidMixin


class File(TimeStampedModel, UuidMixin):
    name = models.CharField(
        max_length=255, unique=True, blank=False, null=False, db_index=True
    )
    content = models.BinaryField(blank=False, null=False)
    size = models.PositiveIntegerField(blank=False, null=False)
    mime_type = models.CharField(max_length=100, blank=True)
    is_public = models.BooleanField(default=False)
