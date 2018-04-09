import django_filters
from django.conf import settings

from waldur_core.core import filters as core_filters
from waldur_core.structure import models as structure_models, filters as structure_filters

from . import models


class IssueFilter(django_filters.FilterSet):
    summary = django_filters.CharFilter(lookup_expr='icontains')

    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')

    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')

    reporter_name = django_filters.CharFilter(lookup_expr='icontains', name='reporter__name')
    reporter = core_filters.URLFilter(view_name='support-user-detail', name='reporter__uuid')

    caller_full_name = django_filters.CharFilter(lookup_expr='icontains', name='caller__full_name')
    caller = core_filters.URLFilter(view_name='user-detail', name='caller__uuid')

    assignee_name = django_filters.CharFilter(lookup_expr='icontains', name='assignee__name')
    assignee = core_filters.URLFilter(view_name='support-user-detail', name='assignee__uuid')

    o = django_filters.OrderingFilter(
        fields=(
            ('created', 'created'),
            ('modified', 'modified'),
            ('type', 'type'),
            ('key', 'key'),
            ('status', 'status'),
            ('priority', 'priority'),
            ('summary', 'summary'),
            ('customer__name', 'customer_name'),
            ('project__name', 'project_name'),
            ('caller__full_name', 'caller_full_name'),
            ('reporter__name', 'reporter_name'),
            ('assignee__name', 'assignee_name'),
        ))

    class Meta(object):
        model = models.Issue
        fields = [
            'key',
            'type',
            'status',
        ]


class IssueResourceFilterBackend(core_filters.GenericKeyFilterBackend):

    content_type_field = 'resource_content_type'
    object_id_field = 'resource_object_id'

    def get_related_models(self):
        return structure_models.ResourceMixin.get_all_models()

    def get_field_name(self):
        return 'resource'


class CommentIssueResourceFilterBackend(IssueResourceFilterBackend):

    content_type_field = 'issue__resource_content_type'
    object_id_field = 'issue__resource_object_id'


class IssueCallerOrRoleFilterBackend(structure_filters.GenericRoleFilter):
    def filter_queryset(self, request, queryset, view):
        return super(IssueCallerOrRoleFilterBackend, self).filter_queryset(request, queryset, view).distinct() | \
            queryset.filter(caller=request.user).distinct()


class CommentIssueCallerOrRoleFilterBackend(structure_filters.GenericRoleFilter):
    def filter_queryset(self, request, queryset, view):
        return super(CommentIssueCallerOrRoleFilterBackend, self).filter_queryset(request,
                                                                                  queryset,
                                                                                  view).distinct() | \
            queryset.filter(issue__caller=request.user).distinct()


class CommentFilter(django_filters.FilterSet):
    description = django_filters.CharFilter(lookup_expr='icontains')
    issue = core_filters.URLFilter(view_name='support-issue-detail', name='issue__uuid')
    issue_uuid = django_filters.UUIDFilter(name='issue__uuid')
    author_name = django_filters.CharFilter(lookup_expr='icontains', name='author__name')
    author_user = core_filters.URLFilter(view_name='user-detail', name='author__user__uuid')

    o = django_filters.OrderingFilter(fields=('created', 'modified'))

    class Meta(object):
        model = models.Comment
        fields = [
            'is_public',
        ]


class SupportUserFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')

    class Meta(object):
        model = models.SupportUser
        fields = ('name', 'user', 'backend_id')


class OfferingFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')
    description = django_filters.CharFilter(lookup_expr='icontains')
    type = django_filters.ChoiceFilter(choices=[(item, item) for item in settings.WALDUR_SUPPORT['OFFERINGS'].keys()])
    issue = core_filters.URLFilter(view_name='support-issue-detail', name='issue__uuid')
    issue_uuid = django_filters.UUIDFilter(name='issue__uuid')
    issue_key = django_filters.CharFilter(name='issue__key')
    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[(representation, representation) for db_value, representation in models.Offering.States.CHOICES],
        choice_mappings={representation: db_value for db_value, representation in models.Offering.States.CHOICES},
    )

    o = django_filters.OrderingFilter(fields=('created', 'modified', 'state'))

    class Meta(object):
        model = models.Offering
        fields = ('name', 'description', 'type', 'issue', 'issue_uuid', 'project', 'project_uuid', 'state')


class AttachmentFilter(django_filters.FilterSet):
    issue = core_filters.URLFilter(view_name='support-issue-detail', name='issue__uuid')
    issue_uuid = django_filters.UUIDFilter(name='issue__uuid')

    class Meta(object):
        model = models.Attachment
        fields = ('issue', 'issue_uuid')
