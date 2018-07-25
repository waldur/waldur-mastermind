from __future__ import unicode_literals

from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import ugettext_lazy as _

from waldur_core.core.models import NameMixin
from waldur_core.monitoring.managers import ResourceSlaManager, ResourceItemManager, ResourceSlaStateTransitionManager


class ScopeMixin(models.Model):
    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    scope = GenericForeignKey('content_type', 'object_id')

    class Meta:
        abstract = True


class ResourceItem(NameMixin, ScopeMixin):
    value = models.FloatField()
    objects = ResourceItemManager()

    class Meta:
        unique_together = ('name', 'content_type', 'object_id')


class ResourceSla(ScopeMixin):
    period = models.CharField(max_length=10)
    value = models.DecimalField(max_digits=11, decimal_places=4, null=True, blank=True)
    agreed_value = models.DecimalField(max_digits=11, decimal_places=4, null=True, blank=True)
    objects = ResourceSlaManager()

    class Meta:
        unique_together = ('period', 'content_type', 'object_id')


class ResourceSlaStateTransition(ScopeMixin):
    period = models.CharField(max_length=10)
    timestamp = models.IntegerField()
    state = models.BooleanField(default=False, help_text=_('If state is True resource became available'))
    objects = ResourceSlaStateTransitionManager()

    class Meta:
        unique_together = ('timestamp', 'period', 'content_type', 'object_id')


class MonitoringModelMixin(models.Model):
    class Meta:
        abstract = True

    sla_items = GenericRelation('monitoring.ResourceSla')
    monitoring_items = GenericRelation('monitoring.ResourceItem')
    state_items = GenericRelation('monitoring.ResourceSlaStateTransition')
