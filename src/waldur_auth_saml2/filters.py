import django_filters

from . import models


class IdentityProviderFilter(django_filters.FilterSet):
    class Meta(object):
        model = models.IdentityProvider
        fields = ('name',)

    name = django_filters.CharFilter(lookup_expr='icontains')
