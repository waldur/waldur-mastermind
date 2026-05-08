from datetime import timedelta

import django_filters
from django.db.models import Count
from django.utils import timezone

from waldur_core.core import filters as core_filters
from waldur_mastermind.marketplace_site_agent import models
from waldur_mastermind.marketplace_site_agent.enums import AgentServiceState


class AgentIdentityFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(field_name="name", lookup_expr="exact")
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering__uuid"
    )
    version = django_filters.CharFilter(field_name="version")
    last_restarted = django_filters.DateTimeFilter(
        lookup_expr="gte", label="Last restarted after"
    )
    orphaned = django_filters.BooleanFilter(
        method="filter_orphaned", label="Has no services"
    )

    class Meta:
        model = models.AgentIdentity
        fields = ("name", "offering_uuid", "version", "last_restarted", "orphaned")

    def filter_orphaned(self, queryset, name, value):
        queryset = queryset.annotate(service_count=Count("agentservice"))
        if value:
            return queryset.filter(service_count=0)
        return queryset.exclude(service_count=0)


class AgentServiceFilter(django_filters.FilterSet):
    identity_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-site-agent-identity-detail", field_name="identity__uuid"
    )
    mode = django_filters.CharFilter(field_name="mode", lookup_expr="exact")
    state = django_filters.MultipleChoiceFilter(choices=AgentServiceState.CHOICES)
    stale = django_filters.BooleanFilter(
        method="filter_stale", label="Inactive for more than 24 hours"
    )
    modified_before = django_filters.DateTimeFilter(
        field_name="modified", lookup_expr="lt", label="Modified before"
    )
    modified_after = django_filters.DateTimeFilter(
        field_name="modified", lookup_expr="gt", label="Modified after"
    )

    class Meta:
        model = models.AgentService
        fields = (
            "identity_uuid",
            "mode",
            "state",
            "stale",
            "modified_before",
            "modified_after",
        )

    def filter_stale(self, queryset, name, value):
        threshold = timezone.now() - timedelta(hours=24)
        if value:
            return queryset.filter(modified__lt=threshold)
        return queryset.exclude(modified__lt=threshold)


class AgentProcessorFilter(django_filters.FilterSet):
    service_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-site-agent-service-detail", field_name="service__uuid"
    )
    backend_type = django_filters.CharFilter(
        field_name="backend_type", lookup_expr="exact"
    )
    backend_version = django_filters.CharFilter(
        field_name="backend_version", lookup_expr="exact"
    )
    last_run = django_filters.DateTimeFilter(lookup_expr="gte", label="Last run after")
    last_run_before = django_filters.DateTimeFilter(
        field_name="last_run", lookup_expr="lt", label="Last run before"
    )
    stale = django_filters.BooleanFilter(
        method="filter_stale", label="Last run more than 1 hour ago"
    )

    class Meta:
        model = models.AgentProcessor
        fields = (
            "service_uuid",
            "backend_type",
            "backend_version",
            "last_run",
            "last_run_before",
            "stale",
        )

    def filter_stale(self, queryset, name, value):
        threshold = timezone.now() - timedelta(hours=1)
        if value:
            return queryset.filter(last_run__lt=threshold)
        return queryset.exclude(last_run__lt=threshold)


class SiteAgentLogFilter(django_filters.FilterSet):
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail",
        field_name="agent_identity__offering__uuid",
    )
    agent_identity_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-site-agent-identity-detail",
        field_name="agent_identity__uuid",
    )
    level = django_filters.CharFilter(field_name="level", lookup_expr="exact")
    timestamp_from = django_filters.NumberFilter(
        field_name="timestamp", lookup_expr="gte"
    )
    timestamp_to = django_filters.NumberFilter(
        field_name="timestamp", lookup_expr="lte"
    )

    class Meta:
        model = models.SiteAgentLog
        fields = (
            "offering_uuid",
            "agent_identity_uuid",
            "level",
            "timestamp_from",
            "timestamp_to",
        )
