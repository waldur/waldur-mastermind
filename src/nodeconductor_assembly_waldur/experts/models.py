from __future__ import unicode_literals

from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from model_utils import FieldTracker

from nodeconductor.structure import models as structure_models
from nodeconductor.core import models as core_models


@python_2_unicode_compatible
class ExpertProvider(core_models.UuidMixin,
                     structure_models.TimeStampedModel):
    customer = models.OneToOneField(structure_models.Customer, related_name='+', on_delete=models.CASCADE)

    class Meta(object):
        verbose_name = _('Expert providers')

    def __str__(self):
        return str(self.customer)

    @classmethod
    def get_url_name(cls):
        return 'expert-provider'


class ExpertRequest(core_models.UuidMixin,
                    core_models.NameMixin,
                    core_models.DescribableMixin,
                    structure_models.StructureLoggableMixin,
                    structure_models.TimeStampedModel):
    class States(object):
        REQUESTED = 'requested'
        RESPONDED = 'responded'
        ACTIVE = 'active'
        CANCELLED = 'cancelled'
        FINISHED = 'finished'

        CHOICES = (
            (REQUESTED, _('Requested')),
            (RESPONDED, _('Responded')),
            (ACTIVE, _('Active')),
            (CANCELLED, _('Cancelled')),
            (FINISHED, _('Finished'))
        )

    project = models.ForeignKey(structure_models.Project, related_name='+', on_delete=models.CASCADE)
    state = models.CharField(default=States.REQUESTED, max_length=30, choices=States.CHOICES)
    type = models.CharField(max_length=255)
    tracker = FieldTracker()

    class Meta:
        ordering = ['-created']

    def get_log_fields(self):
        return super(ExpertRequest, self).get_log_fields() + ('state', 'project')

    @classmethod
    def get_url_name(cls):
        return 'expert-request'

    @property
    def type_label(self):
        offerings = settings.WALDUR_SUPPORT.get('OFFERINGS', {})
        type_settings = offerings.get(self.type, {})
        return type_settings.get('label', None)
