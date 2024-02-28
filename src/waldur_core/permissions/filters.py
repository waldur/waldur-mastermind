import django_filters
from django_filters.widgets import BooleanWidget

from . import models


class RoleFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)

    class Meta:
        model = models.Role
        fields = ["is_active"]
