import django_filters

from waldur_mastermind.marketplace import models as marketplace_models


class GoogleAuthFilter(django_filters.FilterSet):
    has_credentials = django_filters.BooleanFilter(
        method='filter_has_credentials', label='has_credentials'
    )

    class Meta:
        model = marketplace_models.ServiceProvider
        fields = []

    def filter_has_credentials(self, queryset, name, value):
        if value:
            return queryset.exclude(googlecredentials__isnull=True)
        elif value is not None:
            return queryset.filter(googlecredentials__isnull=True)
        return queryset
