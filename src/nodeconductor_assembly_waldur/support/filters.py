import django_filters

from nodeconductor.core import filters as core_filters
from nodeconductor.structure import models as structure_models

from . import models


class IssueFilter(django_filters.FilterSet):
    summary = django_filters.CharFilter(lookup_type='icontains')
    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = core_filters.UUIDFilter(name='customer__uuid')
    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = core_filters.UUIDFilter(name='project__uuid')
    reporter_name = django_filters.CharFilter(lookup_type='icontains', name='reporter__name')
    reporter_user = core_filters.URLFilter(view_name='user-detail', name='reporter__user__uuid')
    caller_name = django_filters.CharFilter(lookup_type='icontains', name='caller__name')
    caller_user = core_filters.URLFilter(view_name='user-detail', name='caller__user__uuid')
    assignee_name = django_filters.CharFilter(lookup_type='icontains', name='assignee__name')
    assignee_user = core_filters.URLFilter(view_name='user-detail', name='assignee__user__uuid')

    class Meta(object):
        model = models.Issue
        fields = [
            'key',
            'type',
            'status',
        ]
        order_by = [
            'created',
            'updated',
            # desc
            '-created',
            '-updated',
        ]


class IssueResourceFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return structure_models.ResourceMixin.get_all_models()

    def get_field_name(self):
        return 'resource'


class CommentFilter(django_filters.FilterSet):
    description = django_filters.CharFilter(lookup_type='icontains')
    issue = core_filters.URLFilter(view_name='support-issue-detail', name='issue__uuid')
    issue_uuid = core_filters.UUIDFilter(name='issue__uuid')
    author_name = django_filters.CharFilter(lookup_type='icontains', name='author__name')
    author_user = core_filters.URLFilter(view_name='user-detail', name='author__user__uuid')

    class Meta(object):
        model = models.Comment
        fields = [
            'is_public',
        ]
        order_by = [
            'created',
            'updated',
            # desc
            '-created',
            '-updated',
        ]
