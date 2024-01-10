from django.db import models

from waldur_core.core import mixins as core_mixins
from waldur_core.core import models as core_models


class DataciteReferral(core_models.UuidMixin, core_mixins.ScopeMixin):
    class Meta:
        ordering = (
            "relation_type",
            "published",
        )

    pid = models.CharField(max_length=255, blank=True)
    relation_type = models.CharField(max_length=255, blank=True)
    resource_type = models.CharField(max_length=255, blank=True)
    creator = models.CharField(max_length=255, blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=255, blank=True)
    published = models.CharField(
        max_length=255, blank=True
    )  # seems that this is typically just a year
    referral_url = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.scope} | {self.pid}"
