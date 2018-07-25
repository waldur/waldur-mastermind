import django_filters

from . import models


class ProfileFilter(django_filters.FilterSet):
    user = django_filters.UUIDFilter(name='user__uuid')

    class Meta(object):
        model = models.Profile
        fields = []
