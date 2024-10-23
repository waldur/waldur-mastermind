import django_filters
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django_filters.widgets import BooleanWidget
from rest_framework import filters

from waldur_core.core import filters as core_filters
from waldur_core.core import serializers as core_serializers
from waldur_core.core.mixins import ScopeMixin
from waldur_core.logging import models, utils
from waldur_core.logging.loggers import expand_event_groups


class BaseHookFilter(django_filters.FilterSet):
    author_uuid = django_filters.UUIDFilter(field_name="user__uuid")
    author_fullname = django_filters.CharFilter(
        method="filter_by_full_name", label="User full name contains"
    )
    query = django_filters.CharFilter(method="filter_by_author_query")
    author_username = django_filters.CharFilter(field_name="user__username")
    author_email = django_filters.CharFilter(field_name="user__email")
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)
    last_published = django_filters.DateTimeFilter()

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


class HookSummaryFilterBackend(core_filters.SummaryFilter):
    def get_queryset_filter(self, queryset):
        if queryset.model == models.WebHook:
            return WebHookFilter
        elif queryset.model == models.EmailHook:
            return EmailHookFilter

        return BaseHookFilter

    def get_base_filter(self):
        return BaseHookFilter


class EventFilter(django_filters.FilterSet):
    created_from = core_filters.TimestampFilter(field_name="created", lookup_expr="gte")
    created_to = core_filters.TimestampFilter(field_name="created", lookup_expr="lt")
    message = django_filters.CharFilter(lookup_expr="icontains")
    o = django_filters.OrderingFilter(fields=("created",))

    class Meta:
        model = models.Event
        fields = []


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
            events = models.Feed.objects.filter(
                content_type=content_type,
                object_id=scope.id,
            ).values_list("event_id", flat=True)

            # Include scope if it exists:
            if isinstance(scope, ScopeMixin) and scope.content_type and scope.object_id:
                events |= models.Feed.objects.filter(
                    content_type=scope.content_type,
                    object_id=scope.object_id,
                ).values_list("event_id", flat=True)
            queryset = queryset.filter(id__in=events)

        elif not request.user.is_staff and not request.user.is_support:
            # If user is not staff nor support, he is allowed to see
            # events related to particular scope only.
            queryset = queryset.none()

        return queryset
