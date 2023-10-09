import django_filters

from . import models


class LexisLinkFilter(django_filters.FilterSet):
    uuid = django_filters.UUIDFilter(field_name='uuid')

    class Meta:
        model = models.LexisLink
        fields = ('uuid',)
