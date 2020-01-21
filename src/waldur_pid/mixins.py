from django.db import models


class DataciteMixin(models.Model):
    class Meta:
        abstract = True

    datacite_doi = models.CharField(max_length=255, blank=True)
