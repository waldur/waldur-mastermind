from django.db import models

from waldur_core.core.managers import GenericKeyMixin


class QuotaManager(GenericKeyMixin, models.Manager):
    pass
