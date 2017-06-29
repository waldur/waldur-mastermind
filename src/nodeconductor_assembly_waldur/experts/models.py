from __future__ import unicode_literals

from django.utils.translation import ugettext_lazy as _
from django.db import models

from nodeconductor.structure import models as structure_models
from nodeconductor.core import models as core_models


class ExpertProvider(core_models.UuidMixin,
                     structure_models.TimeStampedModel):
    customer = models.OneToOneField(structure_models.Customer, related_name='+', on_delete=models.CASCADE)

    class Meta(object):
        verbose_name = _('Expert providers')

    def __str__(self):
        return str(self.customer)
