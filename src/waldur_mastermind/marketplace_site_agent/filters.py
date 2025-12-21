import django_filters

from waldur_mastermind.marketplace_site_agent import models
from waldur_mastermind.marketplace_site_agent.enums import AgentServiceState


class AgentIdentityFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(field_name="name", lookup_expr="exact")
    offering_uuid = django_filters.UUIDFilter(field_name="offering__uuid")
    version = django_filters.CharFilter(field_name="version")
    last_restarted = django_filters.DateTimeFilter(
        lookup_expr="gte", label="Last restarted after"
    )

    class Meta:
        model = models.AgentIdentity
        fields = ("name", "offering_uuid", "version", "last_restarted")


class AgentServiceFilter(django_filters.FilterSet):
    identity_uuid = django_filters.UUIDFilter(field_name="identity__uuid")
    mode = django_filters.CharFilter(field_name="mode", lookup_expr="exact")
    state = django_filters.MultipleChoiceFilter(choices=AgentServiceState.CHOICES)

    class Meta:
        model = models.AgentService
        fields = ("identity_uuid", "mode", "state")


class AgentProcessorFilter(django_filters.FilterSet):
    service_uuid = django_filters.UUIDFilter(field_name="service__uuid")
    backend_type = django_filters.CharFilter(
        field_name="backend_type", lookup_expr="exact"
    )
    backend_version = django_filters.CharFilter(
        field_name="backend_version", lookup_expr="exact"
    )
    last_run = django_filters.DateTimeFilter(lookup_expr="gte", label="Last run after")

    class Meta:
        model = models.AgentProcessor
        fields = ("service_uuid", "backend_type", "backend_version", "last_run")
