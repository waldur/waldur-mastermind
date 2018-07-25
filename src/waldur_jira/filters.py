import django_filters
from django.db.models import Q

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters

from . import models


class ProjectTemplateFilter(structure_filters.BaseServicePropertyFilter):
    class Meta(structure_filters.BaseServicePropertyFilter.Meta):
        model = models.ProjectTemplate


class ProjectFilter(structure_filters.BaseResourceFilter):
    class Meta(structure_filters.BaseResourceFilter.Meta):
        model = models.Project


class IssueTypeFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.IssueType


class PriorityFilter(structure_filters.ServicePropertySettingsFilter):
    class Meta(structure_filters.ServicePropertySettingsFilter.Meta):
        model = models.Priority


class IssueFilter(django_filters.FilterSet):
    created_before = django_filters.IsoDateTimeFilter(name="created", lookup_expr="lte")
    created_after = django_filters.IsoDateTimeFilter(name="created", lookup_expr="gte")
    summary = django_filters.CharFilter(lookup_expr='icontains')
    description = django_filters.CharFilter(lookup_expr='icontains')
    jira_project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    jira_project_uuid = django_filters.UUIDFilter(name='project__uuid')
    priority_name = django_filters.ModelMultipleChoiceFilter(
        name='priority__name',
        to_field_name='name',
        queryset=models.Priority.objects.all()
    )
    project = core_filters.URLFilter(view_name='project-detail', name='project__service_project_link__project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__service_project_link__project__uuid')
    type_name = django_filters.CharFilter(name='type__name')
    updated_before = django_filters.IsoDateTimeFilter(name="updated", lookup_expr="lte")
    updated_after = django_filters.IsoDateTimeFilter(name="updated", lookup_expr="gte")
    user_uuid = django_filters.UUIDFilter(name='user__uuid')
    key = django_filters.CharFilter(name='backend_id')
    status = core_filters.LooseMultipleChoiceFilter()
    sla_ttr_breached = django_filters.BooleanFilter(name='resolution_sla', method='filter_resolution_sla',
                                                    widget=django_filters.widgets.BooleanWidget())

    def filter_resolution_sla(self, queryset, name, value):
        if value:
            return queryset.exclude(Q(resolution_sla__gte=0) | Q(resolution_sla__isnull=True))
        else:
            return queryset.filter(resolution_sla__gte=0)

    class Meta(object):
        model = models.Issue
        fields = [
            'description',
            'key',
            'status',
            'summary',
            'user_uuid',
            'creator_name',
            'assignee_name',
            'reporter_name',
        ]
        order_by = [
            'created',
            'updated',
            # desc
            '-created',
            '-updated',
        ]


class CommentFilter(django_filters.FilterSet):
    issue = core_filters.URLFilter(view_name='jira-issues-detail', name='issue__uuid')
    issue_uuid = django_filters.UUIDFilter(name='issue__uuid')
    user_uuid = django_filters.UUIDFilter(name='user__uuid')

    class Meta(object):
        model = models.Comment
        fields = []


class AttachmentFilter(django_filters.FilterSet):
    issue = core_filters.URLFilter(view_name='jira-issues-detail', name='issue__uuid')
    issue_uuid = django_filters.UUIDFilter(name='issue__uuid')

    class Meta(object):
        model = models.Attachment
        fields = []
