import django_filters

from nodeconductor.core.filters import UUIDFilter

from . import models


class PackageTemplateFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_type='icontains')
    settings_uuid = UUIDFilter(name='service_settings__uuid')

    class Meta(object):
        model = models.PackageTemplate
        fields = ('name', 'settings_uuid',)


class OpenStackPackageFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_type='icontains')
    settings_uuid = UUIDFilter(name='service_settings__uuid')

    class Meta(object):
        model = models.OpenStackPackage
        fields = ('name', 'settings_uuid',)
