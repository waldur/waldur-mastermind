import django_filters

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class ServiceProjectLinkFilter(structure_filters.BaseServiceProjectLinkFilter):
    service = core_filters.URLFilter(
        view_name='rancher-detail', field_name='service__uuid'
    )

    class Meta(structure_filters.BaseServiceProjectLinkFilter.Meta):
        model = models.RancherServiceProjectLink


class ClusterFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Cluster


class NodeFilter(django_filters.FilterSet):
    cluster_uuid = django_filters.UUIDFilter(field_name='cluster__uuid')

    class Meta:
        model = models.Node
        fields = ('cluster_uuid',)


class ProjectFilter(structure_filters.ServicePropertySettingsFilter):
    cluster_uuid = django_filters.UUIDFilter(field_name='cluster__uuid')

    class Meta:
        model = models.Project
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'cluster_uuid',
        )


class NamespaceFilter(structure_filters.ServicePropertySettingsFilter):
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')

    class Meta:
        model = models.Namespace
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'project_uuid',
        )


class TemplateFilter(structure_filters.ServicePropertySettingsFilter):
    catalog_uuid = django_filters.UUIDFilter(field_name='catalog__uuid')
    cluster_uuid = django_filters.UUIDFilter(field_name='cluster__uuid')
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')

    class Meta:
        model = models.Template
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'catalog_uuid',
            'cluster_uuid',
            'project_uuid',
        )
