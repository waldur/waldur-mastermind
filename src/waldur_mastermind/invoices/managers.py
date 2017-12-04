from django.db import models as django_models

from waldur_core.core import managers as core_managers


class GenericInvoiceItemManager(core_managers.GenericKeyMixin, django_models.Manager):
    pass
