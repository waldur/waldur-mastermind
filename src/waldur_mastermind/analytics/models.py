from __future__ import unicode_literals

from django.db import models

from django.contrib.contenttypes import fields as ct_fields
from django.contrib.contenttypes import models as ct_models

from waldur_core.core.managers import GenericKeyMixin


class QuotaManager(GenericKeyMixin, models.Manager):
    def update_or_create_quota(self, scope, name, date, usage):
        content_type = ct_models.ContentType.objects.get_for_model(scope)
        return self.update_or_create(
            content_type=content_type,
            object_id=scope.pk,
            name=name,
            date=date,
            defaults=dict(usage=usage),
        )


class DailyQuotaHistory(models.Model):
    """
    This model stores quota usage history per day.
    It is designed to store derived data optimized for dashboard charts.
    See also related design pattern:
    https://martinfowler.com/bliki/ReportingDatabase.html
    """
    content_type = models.ForeignKey(ct_models.ContentType, null=True)
    object_id = models.PositiveIntegerField(null=True)
    scope = ct_fields.GenericForeignKey('content_type', 'object_id')
    objects = QuotaManager()
    name = models.CharField(max_length=150, db_index=True)
    usage = models.BigIntegerField()
    date = models.DateField()
