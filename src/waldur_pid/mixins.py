from django.db import models


class DataciteMixin(models.Model):
    class Meta:
        abstract = True

    datacite_doi = models.CharField(max_length=255, blank=True)

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
