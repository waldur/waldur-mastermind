import django_filters

from nodeconductor.core import filters as core_filters
from nodeconductor.structure import models as structure_models, filters as structure_filters

from . import models


class IssueFilter(django_filters.FilterSet):
    summary = django_filters.CharFilter(lookup_type='icontains')

    customer = core_filters.URLFilter(view_name='customer-detail', name='customer__uuid')
    customer_uuid = django_filters.UUIDFilter(name='customer__uuid')

    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')

    reporter_name = django_filters.CharFilter(lookup_type='icontains', name='reporter__name')
    reporter = core_filters.URLFilter(view_name='support-user-detail', name='reporter__uuid')

    caller_full_name = django_filters.CharFilter(lookup_type='icontains', name='caller__full_name')
    caller = core_filters.URLFilter(view_name='user-detail', name='caller__uuid')

    assignee_name = django_filters.CharFilter(lookup_type='icontains', name='assignee__name')
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


class IssueCallerOrRoleFilterBackend(structure_filters.GenericRoleFilter):
    def filter_queryset(self, request, queryset, view):
        return super(IssueCallerOrRoleFilterBackend, self).filter_queryset(request, queryset, view).distinct() | \
               queryset.filter(caller=request.user).distinct()


class CommentFilter(django_filters.FilterSet):
    description = django_filters.CharFilter(lookup_type='icontains')
    issue = core_filters.URLFilter(view_name='support-issue-detail', name='issue__uuid')
    issue_uuid = django_filters.UUIDFilter(name='issue__uuid')
    author_name = django_filters.CharFilter(lookup_type='icontains', name='author__name')
    author_user = core_filters.URLFilter(view_name='user-detail', name='author__user__uuid')

    o = django_filters.OrderingFilter(fields=('created', 'modified'))

    class Meta(object):
        model = models.Comment
        fields = [
            'is_public',
        ]


class SupportUserFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_type='icontains')

    class Meta(object):
        model = models.SupportUser


class OfferingFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_type='icontains')
    description = django_filters.CharFilter(lookup_type='icontains')
    type = django_filters.CharFilter(lookup_type='icontains')
    issue = core_filters.URLFilter(view_name='support-issue-detail', name='issue__uuid')
    issue_uuid = django_filters.UUIDFilter(name='issue__uuid')
    project = core_filters.URLFilter(view_name='project-detail', name='project__uuid')
    project_uuid = django_filters.UUIDFilter(name='project__uuid')
    state = core_filters.MappedMultipleChoiceFilter(
        choices=[(representation, representation) for db_value, representation in models.Offering.States.CHOICES],
        choice_mappings={representation: db_value for db_value, representation in models.Offering.States.CHOICES},
    )

    o = django_filters.OrderingFilter(fields=('created', 'modified', 'project_name'))

    class Meta(object):
        model = models.Offering
        fields = [
            'name',
            'description',
            'type',
            'issue',
            'project',
            'state',
        ]
