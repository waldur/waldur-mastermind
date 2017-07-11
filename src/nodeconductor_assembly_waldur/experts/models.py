from __future__ import unicode_literals

from decimal import Decimal
from django.conf import settings
from django.core.validators import MinValueValidator
from django.utils.translation import ugettext_lazy as _
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from model_utils import FieldTracker

from nodeconductor.structure import models as structure_models
from nodeconductor.core import models as core_models
from . import managers


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


@python_2_unicode_compatible
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

    user = models.ForeignKey(core_models.User, related_name='+', on_delete=models.CASCADE,
                             help_text=_('The user which has created this request.'))
    project = models.ForeignKey(structure_models.Project, related_name='+', on_delete=models.CASCADE)
    state = models.CharField(default=States.REQUESTED, max_length=30, choices=States.CHOICES)
    type = models.CharField(max_length=255)
    tracker = FieldTracker()
    objects = managers.ExpertRequestManager()

    class Meta:
        ordering = ['-created']

    def get_log_fields(self):
        return super(ExpertRequest, self).get_log_fields() + ('state', 'project', 'user')

    @classmethod
    def get_url_name(cls):
        return 'expert-request'

    @property
    def type_label(self):
        offerings = settings.WALDUR_SUPPORT.get('OFFERINGS', {})
        type_settings = offerings.get(self.type, {})
        return type_settings.get('label', None)

    def __str__(self):
        return '{} / {}'.format(self.project.name, self.project.customer.name)


class ExpertBid(core_models.UuidMixin,
                structure_models.StructureLoggableMixin,
                structure_models.TimeStampedModel):
    user = models.ForeignKey(core_models.User, related_name='+', on_delete=models.CASCADE,
                             help_text=_('The user which has created this bid.'))
    request = models.ForeignKey(ExpertRequest, on_delete=models.CASCADE)
    team = models.ForeignKey(structure_models.Project)
    price = models.DecimalField(max_digits=22, decimal_places=7,
                                validators=[MinValueValidator(Decimal('0'))],
                                default=0)
    objects = managers.ExpertBidManager()

    class Meta:
        ordering = ['-created']

    def get_log_fields(self):
        return super(ExpertBid, self).get_log_fields() + ('request', 'user', 'price')

    @classmethod
    def get_url_name(cls):
        return 'expert-bid'
