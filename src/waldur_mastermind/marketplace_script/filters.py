import django_filters

from . import models


class DryRunFilter(django_filters.FilterSet):
    uuid = django_filters.UUIDFilter(field_name='uuid')

    class Meta:
        model = models.DryRun
        fields = ('uuid',)
