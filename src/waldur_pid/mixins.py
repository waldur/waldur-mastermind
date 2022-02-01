from functools import lru_cache

from django.apps import apps
from django.db import models
from django.utils.translation import gettext_lazy as _


class DataciteMixin(models.Model):
    """
    A marker model for models that can be registered with PIDs and referred to in a Datacite PID way.
    """

    class Meta:
        abstract = True

    datacite_doi = models.CharField(
        max_length=255, blank=True, verbose_name='Datacite DOI'
    )

    # `-1` - citations have never been looked up
    # non-negative value - the number of citations of a DOI
    citation_count = models.IntegerField(
        default=-1, help_text=_('Number of citations of a DOI'),
    )

    error_message = models.TextField(blank=True)

    @classmethod
    @lru_cache(maxsize=1)
    def get_all_models(cls):
        return [model for model in apps.get_models() if issubclass(model, cls)]

    def get_datacite_title(self):
        raise NotImplementedError

    def get_datacite_creators_name(self):
        raise NotImplementedError

    def get_datacite_description(self):
        raise NotImplementedError

    def get_datacite_publication_year(self):
        raise NotImplementedError

    def get_datacite_url(self):
        raise NotImplementedError
