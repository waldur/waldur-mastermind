import django_filters
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from waldur_core.structure import filters as structure_filters

from . import models


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
    cluster_uuid = django_filters.UUIDFilter(field_name='project__cluster__uuid')
    o = django_filters.OrderingFilter(
        fields=(
            ('name', 'name'),
            ('project__cluster__name', 'cluster_name'),
            ('project__name', 'project_name'),
        )
    )

    class Meta:
        model = models.Namespace
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'project_uuid',
            'cluster_uuid',
        )


class TemplateFilter(structure_filters.ServicePropertySettingsFilter):
    catalog_uuid = django_filters.UUIDFilter(field_name='catalog__uuid')
    cluster_uuid = django_filters.UUIDFilter(method='filter_by_cluster')
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    o = django_filters.OrderingFilter(
        fields=(('name', 'name'), ('catalog__name', 'catalog_name'))
    )

    class Meta:
        model = models.Template
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'catalog_uuid',
            'cluster_uuid',
            'project_uuid',
        )

    def filter_by_cluster(self, queryset, name, value):
        try:
            cluster = models.Cluster.objects.get(uuid=value)
        except models.Cluster.DoesNotExist:
            return queryset.none()
        else:
            # Include global templates
            service_settings = cluster.service_settings
            ctype = ContentType.objects.get_for_model(service_settings)
            global_subquery = Q(
                catalog__content_type=ctype, catalog__object_id=service_settings.id
            )
            return queryset.filter(Q(cluster=cluster) | global_subquery)


class UserFilter(django_filters.FilterSet):
    cluster_uuid = django_filters.UUIDFilter(method='filter_by_cluster')
    user_uuid = django_filters.UUIDFilter(field_name='user__uuid')
    user_username = django_filters.CharFilter(
        field_name='user__username', lookup_expr='icontains'
    )
    user_full_name = django_filters.CharFilter(
        field_name='user__full_name', lookup_expr='icontains'
    )
    settings_uuid = django_filters.UUIDFilter(field_name='settings__uuid')

    class Meta:
        model = models.RancherUser
        fields = (
            'cluster_uuid',
            'user_uuid',
            'settings_uuid',
            'is_active',
        )

    def filter_by_cluster(self, queryset, name, value):
        try:
            cluster = models.Cluster.objects.get(uuid=value)
        except models.Cluster.DoesNotExist:
            return queryset.none()
        else:
            user_ids = models.RancherUserClusterLink.objects.filter(
                cluster=cluster
            ).values_list('user_id', flat=True)
            return queryset.filter(id__in=user_ids)


class WorkloadFilter(structure_filters.ServicePropertySettingsFilter):
    cluster_uuid = django_filters.UUIDFilter(field_name='cluster__uuid')
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    namespace_uuid = django_filters.UUIDFilter(field_name='namespace__uuid')
    o = django_filters.OrderingFilter(
        fields=(
            ('name', 'name'),
            ('project__name', 'project_name'),
            ('cluster__name', 'cluster_name'),
            ('namespace__name', 'namespace_name'),
        )
    )

    class Meta:
        model = models.Workload
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'cluster_uuid',
            'project_uuid',
            'namespace_uuid',
        )


class HPAFilter(structure_filters.ServicePropertySettingsFilter):
    cluster_uuid = django_filters.UUIDFilter(field_name='cluster__uuid')
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    namespace_uuid = django_filters.UUIDFilter(field_name='namespace__uuid')
    workload_uuid = django_filters.UUIDFilter(field_name='workload__uuid')

    class Meta:
        model = models.HPA
        fields = structure_filters.ServicePropertySettingsFilter.Meta.fields + (
            'cluster_uuid',
            'project_uuid',
            'namespace_uuid',
            'workload_uuid',
        )


class ApplicationFilter(structure_filters.BaseResourceFilter):
    cluster_uuid = django_filters.UUIDFilter(field_name='cluster__uuid')
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')
    template_uuid = django_filters.UUIDFilter(field_name='template__uuid')
    namespace_uuid = django_filters.UUIDFilter(field_name='namespace__uuid')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Application


class IngressFilter(structure_filters.BaseResourceFilter):
    cluster_uuid = django_filters.UUIDFilter(field_name='cluster__uuid')
    rancher_project_uuid = django_filters.UUIDFilter(field_name='rancher_project__uuid')
    namespace_uuid = django_filters.UUIDFilter(field_name='namespace__uuid')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Ingress


class ServiceFilter(structure_filters.BaseResourceFilter):
    cluster_uuid = django_filters.UUIDFilter(
        field_name='namespace__project__cluster__uuid'
    )
    rancher_project_uuid = django_filters.UUIDFilter(
        field_name='namespace__project__uuid'
    )
    namespace_uuid = django_filters.UUIDFilter(field_name='namespace__uuid')

    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Service
