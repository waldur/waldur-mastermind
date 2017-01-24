import django_filters

from . import models


class PackageTemplateFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_type='icontains')
    settings_uuid = django_filters.UUIDFilter(name='service_settings__uuid')

    class Meta(object):
        model = models.PackageTemplate
        fields = ('name', 'settings_uuid',)


class OpenStackPackageFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_type='icontains')
    customer = django_filters.UUIDFilter(name='tenant__service_project_link__project__customer__uuid')
    project = django_filters.UUIDFilter(name='tenant__service_project_link__project__uuid')
    tenant = django_filters.UUIDFilter(name='tenant__uuid')

    class Meta(object):
        model = models.OpenStackPackage
        fields = ('name', 'customer', 'project', 'tenant')
