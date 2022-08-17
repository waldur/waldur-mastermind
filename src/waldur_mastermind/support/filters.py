import collections

import django_filters
from django.contrib.contenttypes.models import ContentType

from waldur_core.core import filters as core_filters
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_openstack.openstack_tenant import models as openstack_tenant_models

from . import models


class KeyOrderingFilter(django_filters.OrderingFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra['choices'] += [
            ('key', 'Key'),
            ('-key', 'Key (descending)'),
        ]

    def filter(self, qs, value):
        if isinstance(value, collections.Iterable) and any(
            v in ['key', '-key'] for v in value
        ):
            qs = qs.extra(
                select={'num_key': r"COALESCE(substring(key from '\d+'), '0')::int"}
            )
            if 'key' in value:
                return super().filter(qs, ['num_key'])

            return super().filter(qs, ['-num_key'])

        return super().filter(qs, value)


class IssueFilter(django_filters.FilterSet):
    summary = django_filters.CharFilter(lookup_expr='icontains')

    customer = core_filters.URLFilter(
        view_name='customer-detail', field_name='customer__uuid'
    )
    customer_uuid = django_filters.UUIDFilter(field_name='customer__uuid')

    project = core_filters.URLFilter(
        view_name='project-detail', field_name='project__uuid'
    )
    project_uuid = django_filters.UUIDFilter(field_name='project__uuid')

    reporter_name = django_filters.CharFilter(
        lookup_expr='icontains', field_name='reporter__name'
    )
    reporter = core_filters.URLFilter(
        view_name='support-user-detail', field_name='reporter__uuid'
    )

    caller_full_name = django_filters.CharFilter(
        method='filter_by_full_name', label='Caller full name contains'
    )
    caller = core_filters.URLFilter(view_name='user-detail', field_name='caller__uuid')

    assignee_name = django_filters.CharFilter(
        lookup_expr='icontains', field_name='assignee__name'
    )
    assignee = core_filters.URLFilter(
        view_name='support-user-detail', field_name='assignee__uuid'
    )
    resource_uuid = django_filters.UUIDFilter(
        method='filter_by_resource_uuid', label='Resource UUID'
    )
    resource_external_ip = django_filters.CharFilter(
        method='filter_by_resource_external_ip', label='Resource external IP'
    )
    resource_internal_ip = django_filters.CharFilter(
        method='filter_by_resource_internal_ip', label='Resource internal IP'
    )
    remote_id = django_filters.CharFilter(
        lookup_expr='icontains', field_name='remote_id'
    )

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value, 'caller')

    def filter_by_resource_uuid(self, queryset, name, value):
        related_models = structure_models.BaseResource.get_all_models() + [
            marketplace_models.Resource
        ]
        ids = []

        for related_model in related_models:
            ids += related_model.objects.filter(uuid=value).values_list('id', flat=True)

        return queryset.filter(resource_object_id__in=ids)

    def filter_by_resource_external_ip(self, queryset, name, value):
        instance_ids = openstack_tenant_models.FloatingIP.objects.filter(
            address=value
        ).values_list('internal_ip__instance_id', flat=True)
        content_type = ContentType.objects.get_for_model(
            openstack_tenant_models.Instance
        )
        return queryset.filter(
            resource_object_id__in=instance_ids, resource_content_type=content_type
        )

    def filter_by_resource_internal_ip(self, queryset, name, value):
        instance_ids = openstack_tenant_models.InternalIP.objects.filter(
            fixed_ips__icontains=value, instance_id__isnull=False
        ).values_list('instance_id', flat=True)
        content_type = ContentType.objects.get_for_model(
            openstack_tenant_models.Instance
        )
        return queryset.filter(
            resource_object_id__in=instance_ids, resource_content_type=content_type
        )

    o = KeyOrderingFilter(
        fields=(
            ('created', 'created'),
            ('modified', 'modified'),
            ('type', 'type'),
            ('status', 'status'),
            ('priority', 'priority'),
            ('summary', 'summary'),
            ('customer__name', 'customer_name'),
            ('project__name', 'project_name'),
            ('caller__first_name', 'caller_first_name'),
            ('caller__last_name', 'caller_last_name'),
            ('reporter__name', 'reporter_name'),
            ('assignee__name', 'assignee_name'),
            ('remote_id', 'remote_id'),
        )
    )

    class Meta:
        model = models.Issue
        fields = [
            'key',
            'type',
            'status',
        ]


class PriorityFilter(structure_filters.NameFilterSet):
    class Meta:
        model = models.Priority
        fields = ('name', 'name_exact')


class IssueResourceFilterBackend(core_filters.GenericKeyFilterBackend):

    content_type_field = 'resource_content_type'
    object_id_field = 'resource_object_id'

    def get_related_models(self):
        from waldur_mastermind.marketplace.models import Resource

        return structure_models.BaseResource.get_all_models() + [Resource]

    def get_field_name(self):
        return 'resource'


class CommentIssueResourceFilterBackend(IssueResourceFilterBackend):

    content_type_field = 'issue__resource_content_type'
    object_id_field = 'issue__resource_object_id'


class IssueCallerOrRoleFilterBackend(structure_filters.GenericRoleFilter):
    def filter_queryset(self, request, queryset, view):
        return (
            super(IssueCallerOrRoleFilterBackend, self)
            .filter_queryset(request, queryset, view)
            .distinct()
            | queryset.filter(caller=request.user).distinct()
        )


class CommentIssueCallerOrRoleFilterBackend(structure_filters.GenericRoleFilter):
    def filter_queryset(self, request, queryset, view):
        return (
            super(CommentIssueCallerOrRoleFilterBackend, self)
            .filter_queryset(request, queryset, view)
            .distinct()
            | queryset.filter(issue__caller=request.user).distinct()
        )


class CommentFilter(django_filters.FilterSet):
    description = django_filters.CharFilter(lookup_expr='icontains')
    issue = core_filters.URLFilter(
        view_name='support-issue-detail', field_name='issue__uuid'
    )
    issue_uuid = django_filters.UUIDFilter(field_name='issue__uuid')
    author_name = django_filters.CharFilter(
        lookup_expr='icontains', field_name='author__name'
    )
    author_user = core_filters.URLFilter(
        view_name='user-detail', field_name='author__user__uuid'
    )

    o = django_filters.OrderingFilter(fields=('created', 'modified'))

    class Meta:
        model = models.Comment
        fields = [
            'is_public',
        ]


class SupportUserFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr='icontains')

    class Meta:
        model = models.SupportUser
        fields = ('name', 'user', 'backend_id')


class AttachmentFilter(django_filters.FilterSet):
    issue = core_filters.URLFilter(
        view_name='support-issue-detail', field_name='issue__uuid'
    )
    issue_uuid = django_filters.UUIDFilter(field_name='issue__uuid')

    class Meta:
        model = models.Attachment
        fields = ('issue', 'issue_uuid')


class FeedbackFilter(django_filters.FilterSet):
    issue = core_filters.URLFilter(
        view_name='support-issue-detail', field_name='issue__uuid'
    )
    issue_uuid = django_filters.UUIDFilter(field_name='issue__uuid')

    user = core_filters.URLFilter(
        view_name='user-detail', field_name='issue__caller__uuid'
    )
    user_uuid = django_filters.UUIDFilter(field_name='issue__caller__uuid')

    created_before = django_filters.DateTimeFilter(
        field_name="created", lookup_expr="lte"
    )
    created_after = django_filters.DateTimeFilter(
        field_name="created", lookup_expr="gte"
    )

    evaluation = core_filters.MappedMultipleChoiceFilter(
        choices=[
            (representation, representation)
            for db_value, representation in models.Feedback.Evaluation.CHOICES
        ],
        choice_mappings={
            representation: db_value
            for db_value, representation in models.Feedback.Evaluation.CHOICES
        },
    )

    issue_key = django_filters.CharFilter(field_name='issue__key')
    user_full_name = django_filters.CharFilter(
        method='filter_by_full_name', label='User full name contains'
    )

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value, 'issue__caller')
