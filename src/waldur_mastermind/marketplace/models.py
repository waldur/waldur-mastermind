from __future__ import unicode_literals

import six
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

from waldur_core.core import models as core_models
from waldur_core.structure import models as structure_models


@python_2_unicode_compatible
class ServiceProvider(core_models.UuidMixin,
                      structure_models.TimeStampedModel):
    customer = models.OneToOneField(structure_models.Customer, related_name='+', on_delete=models.CASCADE)
    enable_notifications = models.BooleanField(default=True)

    class Meta(object):
        verbose_name = _('Service provider')

    def __str__(self):
        return six.text_type(self.customer)

    @classmethod
    def get_url_name(cls):
        return 'marketplace-service-provider'
