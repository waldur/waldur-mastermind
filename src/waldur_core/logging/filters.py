import django_filters
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django_filters.widgets import BooleanWidget
from drf_spectacular.plumbing import build_parameter_type
from drf_spectacular.utils import OpenApiParameter
from rest_framework import filters

from waldur_core.core import filters as core_filters
from waldur_core.core import serializers as core_serializers
from waldur_core.core.mixins import ScopeMixin
from waldur_core.logging import models, utils
from waldur_core.logging.event_logger import expand_event_groups


class BaseHookFilter(django_filters.FilterSet):
    author_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="user__uuid"
    )
    author_fullname = django_filters.CharFilter(
        method="filter_by_full_name", label="User full name contains"
    )
    query = django_filters.CharFilter(
        method="filter_by_author_query",
        label="Filter by author name, username and email",
    )
    author_username = django_filters.CharFilter(field_name="user__username")
    author_email = django_filters.CharFilter(field_name="user__email")
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)
    last_published = django_filters.DateTimeFilter()

    class Meta:
        model = models.BaseHook
        fields = []

    def filter_by_full_name(self, queryset, name, value):
        return core_filters.filter_by_full_name(queryset, value, "user")

    def filter_by_author_query(self, queryset, name, value):
        return queryset.filter(
            Q(user__first_name__icontains=value)
            | Q(user__last_name__icontains=value)
            | Q(user__username__icontains=value)
            | Q(user__email__icontains=value)
        ).distinct()


class WebHookFilter(BaseHookFilter):
    class Meta:
        model = models.WebHook
        fields = ("destination_url", "content_type")


class EmailHookFilter(BaseHookFilter):
    class Meta:
        model = models.EmailHook
        fields = ("email",)


class EventFilter(django_filters.FilterSet):
    created_from = core_filters.TimestampFilter(field_name="created", lookup_expr="gte")
    created_to = core_filters.TimestampFilter(field_name="created", lookup_expr="lt")
    message = django_filters.CharFilter(lookup_expr="icontains")
    customer_uuid = core_filters.RelatedUUIDFilter(
        view_name="customer-detail",
        method="filter_customer_uuid",
        label="Customer UUID",
    )
    project_uuid = core_filters.RelatedUUIDFilter(
        view_name="project-detail", method="filter_project_uuid", label="Project UUID"
    )
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", method="filter_user_uuid", label="User UUID"
    )
    o = django_filters.OrderingFilter(fields=("created",))

    class Meta:
        model = models.Event
        fields = []

    def filter_customer_uuid(self, queryset, name, value):
        return queryset.filter(context__customer_uuid=value.hex)

    def filter_project_uuid(self, queryset, name, value):
        return queryset.filter(context__project_uuid=value.hex)

    def filter_user_uuid(self, queryset, name, value):
        return queryset.filter(context__user_uuid=value.hex)


class EventFilterBackend(filters.BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        event_types = request.query_params.getlist("event_type")
        if event_types:
            queryset = queryset.filter(event_type__in=event_types)

        features = request.query_params.getlist("feature")
        if features:
            queryset = queryset.filter(event_type__in=expand_event_groups(features))

        if "scope" in request.query_params:
            field = core_serializers.GenericRelatedField(
                related_models=utils.get_loggable_models()
            )
            field._context = {"request": request}
            scope = field.to_internal_value(request.query_params["scope"])

            # Check permissions
            visible = scope._meta.model.get_permitted_objects(request.user)
            if not visible.filter(pk=scope.pk).exists():
                return queryset.none()

            content_type = ContentType.objects.get_for_model(scope)
            subquery = Q(feed__content_type=content_type, feed__object_id=scope.id)

            # Include scope if it exists:
            if isinstance(scope, ScopeMixin) and scope.content_type and scope.object_id:
                subquery |= Q(
                    feed__content_type=scope.content_type,
                    feed__object_id=scope.object_id,
                )

            queryset = queryset.filter(subquery)

        elif not request.user.is_staff and not request.user.is_support:
            # If user is not staff nor support, he is allowed to see
            # events related to particular scope only.
            queryset = queryset.none()

        return queryset

    def get_schema_operation_parameters(self, view):
        return [
            build_parameter_type(
                name="event_type",
                schema={"type": "array", "items": {"type": "string"}},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by event type. Can be specified multiple times.",
            ),
            build_parameter_type(
                name="feature",
                schema={"type": "array", "items": {"type": "string"}},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by feature (event group). Can be specified multiple times.",
            ),
            build_parameter_type(
                name="scope",
                schema={"type": "string", "format": "uri"},
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by scope URL.",
            ),
        ]


class EventSubscriptionFilter(django_filters.FilterSet):
    o = django_filters.OrderingFilter(fields=["created"])
    user_uuid = django_filters.CharFilter(field_name="user__uuid")
    user_username = django_filters.CharFilter(field_name="user__username")

    class Meta:
        model = models.EventSubscription
        fields = []


class EventSubscriptionQueueFilter(django_filters.FilterSet):
    o = django_filters.OrderingFilter(fields=["created"])
    event_subscription_uuid = core_filters.RelatedUUIDFilter(
        view_name="event-subscription-detail", field_name="event_subscription__uuid"
    )
    offering_uuid = core_filters.RelatedUUIDFilter(
        view_name="marketplace-provider-offering-detail", field_name="offering_uuid"
    )
    object_type = django_filters.CharFilter(field_name="object_type")

    class Meta:
        model = models.EventSubscriptionQueue
        fields = []


class EmailLogFilter(django_filters.FilterSet):
    subject = django_filters.CharFilter(lookup_expr="icontains")
    body = django_filters.CharFilter(lookup_expr="icontains")
    emails = django_filters.CharFilter(lookup_expr="icontains")
    sent_at = django_filters.DateFilter(field_name="sent_at", lookup_expr="date")
    o = django_filters.OrderingFilter(fields=["sent_at", "subject"])

    class Meta:
        model = models.EmailLog
        fields = [
            "sent_at",
            "subject",
            "body",
            "emails",
        ]


class SystemLogFilter(django_filters.FilterSet):
    source = django_filters.ChoiceFilter(choices=models.SystemLog.SourceChoices.choices)
    instance = django_filters.CharFilter(lookup_expr="exact")
    level = django_filters.ChoiceFilter(
        choices=[
            ("INFO", "INFO"),
            ("WARNING", "WARNING"),
            ("ERROR", "ERROR"),
            ("CRITICAL", "CRITICAL"),
        ]
    )
    level_gte = django_filters.NumberFilter(
        field_name="level_number",
        lookup_expr="gte",
        help_text="Min level: 20=INFO, 30=WARNING, 40=ERROR, 50=CRITICAL",
    )
    created_from = core_filters.TimestampFilter(field_name="created", lookup_expr="gte")
    created_to = core_filters.TimestampFilter(field_name="created", lookup_expr="lt")
    logger_name = django_filters.CharFilter(lookup_expr="istartswith")
    message = django_filters.CharFilter(lookup_expr="icontains")
    o = django_filters.OrderingFilter(fields=["created", "level_number", "instance"])

    class Meta:
        model = models.SystemLog
        fields = ["source", "instance", "level", "logger_name"]


class UserDataAccessLogFilter(django_filters.FilterSet):
    """Filter for global data access logs endpoint (staff/support only)."""

    start_date = django_filters.DateFilter(
        field_name="timestamp", lookup_expr="date__gte"
    )
    end_date = django_filters.DateFilter(
        field_name="timestamp", lookup_expr="date__lte"
    )
    accessor_type = django_filters.ChoiceFilter(
        choices=models.UserDataAccessLog.AccessorType.CHOICES
    )
    user_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="target_user__uuid"
    )
    accessor_uuid = core_filters.RelatedUUIDFilter(
        view_name="user-detail", field_name="accessor__uuid"
    )
    query = django_filters.CharFilter(method="filter_by_query")
    o = django_filters.OrderingFilter(
        fields=[
            ("timestamp", "timestamp"),
            ("accessor_type", "accessor_type"),
            ("target_user__username", "user_username"),
            ("accessor__username", "accessor_username"),
        ]
    )

    class Meta:
        model = models.UserDataAccessLog
        fields = []

    def filter_by_query(self, queryset, name, value):
        """Full-text search across user and accessor names."""
        return queryset.filter(
            Q(target_user__username__icontains=value)
            | Q(target_user__first_name__icontains=value)
            | Q(target_user__last_name__icontains=value)
            | Q(target_user__email__icontains=value)
            | Q(accessor__username__icontains=value)
            | Q(accessor__first_name__icontains=value)
            | Q(accessor__last_name__icontains=value)
            | Q(accessor__email__icontains=value)
        ).distinct()
