import django_filters

from waldur_core.core import filters as core_filters

from . import models


class SlurmPackageFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    service_settings = core_filters.URLFilter(
        view_name='servicesettings-detail', field_name='service_settings__uuid')

    class Meta:
        model = models.SlurmPackage
        fields = []
