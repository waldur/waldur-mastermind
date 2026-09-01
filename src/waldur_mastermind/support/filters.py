import collections

import django_filters
from django.db.models import Q

from waldur_core.core import filters as core_filters
from waldur_core.core.resolvers import filter_by_resource_attribute
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support.backend import SupportBackendType

from . import models


class KeyOrderingFilter(django_filters.OrderingFilter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra["choices"] += [
            ("key", "Key"),
            ("-key", "Key (descending)"),
        ]

    def filter(self, qs, value):
        if isinstance(value, collections.abc.Iterable) and any(
            v in ["key", "-key"] for v in value
        ):
            qs = qs.extra(
                select={"num_key": r"COALESCE(substring(key from '\d+'), '0')::int"}
            )
            if "key" in value:
                return super().filter(qs, ["num_key"])

            return super().filter(qs, ["-num_key"])

        return super().filter(qs, value)


class IssueFilter(django_filters.FilterSet):
    summary = django_filters.CharFilter(lookup_expr="icontains")
    query = django_filters.CharFilter(
        method="filter_by_query", label="Summary or key contains"
    )

    customer = core_filters.URLFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail", field_name="customer__uuid"
    )

    project = core_filters.URLFilter(
        view_name="project-detail", field_name="project__uuid"
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", field_name="project__uuid"
    )

    reporter_name = django_filters.CharFilter(
        lookup_expr="icontains", field_name="reporter__name"
    )
    reporter = core_filters.URLFilter(
        view_name="support-user-detail", field_name="reporter__uuid"
    )

    caller_full_name = django_filters.CharFilter(
        method="filter_by_full_name", label="Caller full name contains"
    )
    caller = core_filters.URLFilter(view_name="user-detail", field_name="caller__uuid")

    assignee_name = django_filters.CharFilter(
        lookup_expr="icontains", field_name="assignee__name"
    )
    assignee = core_filters.URLFilter(
        view_name="support-user-detail", field_name="assignee__uuid"
    )
    resource_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-resource-detail",
        method="filter_by_resource_uuid",
        label="Resource UUID",
    )
    # The filter field name MUST match the lookup_key in the resolvers registry.
    resource_external_ip = django_filters.CharFilter(
        method="filter_by_resource_attribute",
        label="Resource external IP",
    )
    resource_internal_ip = django_filters.CharFilter(
        method="filter_by_resource_attribute",
        label="Resource internal IP",
    )
    remote_id = django_filters.CharFilter(
        lookup_expr="icontains", field_name="remote_id"
    )

    sla_breached = django_filters.BooleanFilter(field_name="sla_breached")
    is_routed = django_filters.BooleanFilter(
        method="filter_by_is_routed", label="Has been routed to provider"
    )
    is_escalated = django_filters.BooleanFilter(field_name="is_escalated")
    is_parent = django_filters.BooleanFilter(
        method="filter_by_is_parent", label="Is a parent issue"
    )
    provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="provider-helpdesk-detail", field_name="provider_helpdesk__uuid"
    )

    is_open = django_filters.BooleanFilter(
        method="filter_by_is_open", label="Has not reached a terminal status"
    )

    resolution_year_month = django_filters.CharFilter(
        field_name="resolution_date", method="filter_by_resolution_year_month"
    )

    def filter_by_is_open(self, queryset, name, value):
        # Same definition the support statistics count with, so the dashboard
        # card and the list it links to cannot disagree.
        return queryset.open() if value else queryset.closed()

    def filter_by_resolution_year_month(self, queryset, name, value):
        year, month = value.split("-")
        return queryset.filter(resolution_date__year=year, resolution_date__month=month)

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value, "caller")

    def filter_by_resource_uuid(self, queryset, name, value):
        related_models = structure_models.BaseResource.get_all_models() + [
            marketplace_models.Resource
        ]
        ids = []

        for related_model in related_models:
            ids += related_model.objects.filter(uuid=value).values_list("id", flat=True)

        return queryset.filter(resource_object_id__in=ids)

    def filter_by_resource_attribute(self, queryset, name, value):
        return filter_by_resource_attribute(queryset, name, value)

    def filter_by_is_routed(self, queryset, name, value):
        if value:
            return queryset.filter(child_issues__isnull=False).distinct()
        return queryset.filter(child_issues__isnull=True)

    def filter_by_is_parent(self, queryset, name, value):
        if value:
            return queryset.filter(
                parent_issue__isnull=True, child_issues__isnull=False
            ).distinct()
        return queryset.exclude(parent_issue__isnull=True, child_issues__isnull=False)

    def filter_by_query(self, queryset, name, value):
        return queryset.filter(
            Q(summary__icontains=value) | Q(key__icontains=value)
        ).distinct()

    o = KeyOrderingFilter(
        fields=(
            ("created", "created"),
            ("modified", "modified"),
            ("type", "type"),
            ("status", "status"),
            ("priority", "priority"),
            ("summary", "summary"),
            ("customer__name", "customer_name"),
            ("project__name", "project_name"),
            ("caller__first_name", "caller_first_name"),
            ("caller__last_name", "caller_last_name"),
            ("reporter__name", "reporter_name"),
            ("assignee__name", "assignee_name"),
            ("remote_id", "remote_id"),
        )
    )

    class Meta:
        model = models.Issue
        fields = [
            "key",
            "type",
            "status",
            "is_open",
            "resolution_year_month",
        ]


class PriorityFilter(structure_filters.NameFilterSet):
    class Meta:
        model = models.Priority
        fields = ("name", "name_exact")


class RequestTypeFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()
    name = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = models.RequestType
        fields = ["is_active", "name"]


class IssueResourceFilterBackend(core_filters.GenericKeyFilterBackend):
    content_type_field = "resource_content_type"
    object_id_field = "resource_object_id"

    def get_related_models(self):
        from waldur_mastermind.marketplace.models import Resource

        return structure_models.BaseResource.get_all_models() + [Resource]

    def get_field_name(self):
        return "resource"


class CommentIssueResourceFilterBackend(IssueResourceFilterBackend):
    content_type_field = "issue__resource_content_type"
    object_id_field = "issue__resource_object_id"


class IssueCallerOrRoleFilterBackend(structure_filters.GenericRoleFilter):
    def filter_queryset(self, request, queryset, view):
        return (
            super().filter_queryset(request, queryset, view).distinct()
            # A caller sees their own top-level tickets, but not the internal
            # provider-routed child issues (those are visible to staff/support
            # and to the provider's own support users below).
            | queryset.filter(caller=request.user, parent_issue__isnull=True).distinct()
            # Provider support users can see issues routed to their helpdesk.
            | queryset.filter(
                provider_helpdesk__support_users__user=request.user,
                provider_helpdesk__support_users__is_active=True,
            ).distinct()
        )


class CommentIssueCallerOrRoleFilterBackend(structure_filters.GenericRoleFilter):
    def filter_queryset(self, request, queryset, view):
        return (
            super().filter_queryset(request, queryset, view).distinct()
            | queryset.filter(issue__caller=request.user).distinct()
            # Provider support users can see comments on issues routed to their
            # helpdesk.
            | queryset.filter(
                issue__provider_helpdesk__support_users__user=request.user,
                issue__provider_helpdesk__support_users__is_active=True,
            ).distinct()
        )


class CommentFilter(django_filters.FilterSet):
    description = django_filters.CharFilter(lookup_expr="icontains")
    issue = core_filters.URLFilter(
        view_name="support-issue-detail", field_name="issue__uuid"
    )
    issue_uuid = core_filters.RelatedUUIDFilter(
        view_name="support-issue-detail", field_name="issue__uuid"
    )
    author_name = django_filters.CharFilter(
        lookup_expr="icontains", field_name="author__name"
    )
    author_user = core_filters.URLFilter(
        view_name="user-detail", field_name="author__user__uuid"
    )
    remote_id_is_set = django_filters.BooleanFilter(
        method="filter_by_remote_id_is_set", label="Remote ID is set."
    )

    def filter_by_remote_id_is_set(self, queryset, name, value):
        if value is None:
            return queryset
        elif value:
            return queryset.exclude(remote_id="").exclude(remote_id__isnull=True)
        else:
            return queryset.filter(Q(remote_id="") | Q(remote_id__isnull=True))

    o = django_filters.OrderingFilter(fields=("created", "modified"))

    class Meta:
        model = models.Comment
        fields = ["is_public", "remote_id_is_set"]


class SupportUserFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    backend_name = django_filters.ChoiceFilter(
        choices=[
            (SupportBackendType.ATLASSIAN, "Atlassian"),
            (SupportBackendType.ZAMMAD, "Zammad"),
            (SupportBackendType.SMAX, "SMAX"),
            (SupportBackendType.BASIC, "Basic"),
        ],
        label="Helpdesk",
    )
    query = django_filters.CharFilter(
        method="filter_by_query",
        label="Search by name, backend ID or linked user name/email",
    )
    o = django_filters.OrderingFilter(
        fields=("name", "backend_name", "backend_id", "is_active")
    )

    def filter_by_query(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value)
            | Q(backend_id__icontains=value)
            | Q(user__first_name__icontains=value)
            | Q(user__last_name__icontains=value)
            | Q(user__email__icontains=value)
        )

    class Meta:
        model = models.SupportUser
        fields = ("name", "user", "backend_id", "backend_name", "is_active")


class AttachmentFilter(django_filters.FilterSet):
    issue = core_filters.URLFilter(
        view_name="support-issue-detail", field_name="issue__uuid"
    )
    issue_uuid = core_filters.RelatedUUIDFilter(
        view_name="support-issue-detail", field_name="issue__uuid"
    )

    class Meta:
        model = models.Attachment
        fields = ("issue", "issue_uuid")


class ProviderTicketFilter(django_filters.FilterSet):
    status = django_filters.CharFilter()
    priority = django_filters.CharFilter()
    is_escalated = django_filters.BooleanFilter()
    sla_breached = django_filters.BooleanFilter()
    provider_assignee = core_filters.RelatedUUIDFilter(
        view_name="provider-support-user-detail",
        field_name="provider_assignee__uuid",
    )
    summary = django_filters.CharFilter(lookup_expr="icontains")

    o = django_filters.OrderingFilter(
        fields=(
            ("created", "created"),
            ("modified", "modified"),
            ("priority", "priority"),
            ("status", "status"),
        )
    )

    class Meta:
        model = models.Issue
        fields = ["status", "priority", "is_escalated", "sla_breached"]


class ProviderSupportUserFilter(django_filters.FilterSet):
    provider_helpdesk_uuid = core_filters.RelatedUUIDFilter(
        view_name="provider-helpdesk-detail", field_name="provider_helpdesk__uuid"
    )
    is_active = django_filters.BooleanFilter()
    role = django_filters.CharFilter()
    user_full_name = django_filters.CharFilter(
        method="filter_by_full_name", label="User full name contains"
    )

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value, "user")

    class Meta:
        model = models.ProviderSupportUser
        fields = ["is_active", "role"]


class ProviderCannedResponseFilter(django_filters.FilterSet):
    provider_helpdesk_uuid = core_filters.RelatedUUIDFilter(
        view_name="provider-helpdesk-detail", field_name="provider_helpdesk__uuid"
    )
    category = django_filters.CharFilter(lookup_expr="icontains")
    name = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = models.ProviderCannedResponse
        fields = ["category"]


class ProviderHelpdeskFilter(django_filters.FilterSet):
    service_provider_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-service-provider-detail",
        field_name="service_provider__uuid",
    )
    is_active = django_filters.BooleanFilter()
    backend_type = django_filters.CharFilter()

    class Meta:
        model = models.ProviderHelpdesk
        fields = ["is_active", "backend_type"]


class IssueTagFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = models.IssueTag
        fields = ["name"]


class IssueLinkFilter(django_filters.FilterSet):
    source_uuid = core_filters.RelatedUUIDFilter(
        view_name="support-issue-detail", field_name="source__uuid"
    )
    target_uuid = core_filters.RelatedUUIDFilter(
        view_name="support-issue-detail", field_name="target__uuid"
    )
    link_type = django_filters.CharFilter()

    class Meta:
        model = models.IssueLink
        fields = ["link_type"]


class SavedFilterFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    is_shared = django_filters.BooleanFilter()

    class Meta:
        model = models.SavedFilter
        fields = ["name", "is_shared"]


class CannedResponseFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    category = django_filters.CharFilter(lookup_expr="icontains")
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = models.CannedResponse
        fields = ["name", "category", "is_active"]


class FeedbackFilter(django_filters.FilterSet):
    issue = core_filters.URLFilter(
        view_name="support-issue-detail", field_name="issue__uuid"
    )
    issue_uuid = core_filters.RelatedUUIDFilter(
        view_name="support-issue-detail", field_name="issue__uuid"
    )

    user = core_filters.URLFilter(
        view_name="user-detail", field_name="issue__caller__uuid"
    )
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="issue__caller__uuid"
    )

    created_before = django_filters.DateTimeFilter(
        field_name="created", lookup_expr="lte"
    )
    created_after = django_filters.DateTimeFilter(
        field_name="created", lookup_expr="gte"
    )

    evaluation = django_filters.NumberFilter()

    issue_key = django_filters.CharFilter(field_name="issue__key")
    user_full_name = django_filters.CharFilter(
        method="filter_by_full_name", label="User full name contains"
    )

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value, "issue__caller")
