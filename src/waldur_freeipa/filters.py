import django_filters

from . import models


class ProfileFilter(django_filters.FilterSet):
    user = django_filters.UUIDFilter(field_name='user__uuid')

    class Meta:
        model = models.Profile
        fields = []
