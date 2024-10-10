import django_filters
from django.db.models import Q

from . import models


class ProfileFilter(django_filters.FilterSet):
    user = django_filters.UUIDFilter(field_name="user__uuid")
    query = django_filters.CharFilter(method="filter_query")

    def filter_query(self, queryset, name, value):
        return queryset.filter(
            Q(username__icontains=value)
            | Q(user__username__icontains=value)
            | Q(user__uuid__icontains=value)
            | Q(user__first_name__icontains=value)
            | Q(user__last_name__icontains=value)
        ).distinct()

    class Meta:
        model = models.Profile
        fields = []
