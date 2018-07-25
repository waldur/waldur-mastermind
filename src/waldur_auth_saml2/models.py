from django.db import models

from waldur_core.core import fields as core_fields


class IdentityProvider(models.Model):
    name = models.TextField(db_index=True)
    url = models.URLField()
    metadata = core_fields.JSONField(default=dict)

    class Meta(object):
        ordering = ('name',)
