from __future__ import unicode_literals

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
import django_filters
from django_filters.widgets import BooleanWidget
from rest_framework import filters
from rest_framework.serializers import ValidationError

from waldur_core.core import serializers as core_serializers, filters as core_filters
from waldur_core.core.filters import ExternalFilterBackend
from waldur_core.core.utils import camel_case_to_underscore
from waldur_core.logging import models, utils
from waldur_core.logging.loggers import expand_alert_groups, expand_event_groups


class AlertFilter(django_filters.FilterSet):
    """ Basic filters for alerts """

    acknowledged = django_filters.BooleanFilter(name='acknowledged', distinct=True, widget=BooleanWidget)
    closed_from = core_filters.TimestampFilter(name='closed', lookup_expr='gte')
    closed_to = core_filters.TimestampFilter(name='closed', lookup_expr='lt')
    created_from = core_filters.TimestampFilter(name='created', lookup_expr='gte')
    created_to = core_filters.TimestampFilter(name='created', lookup_expr='lt')
    content_type = core_filters.ContentTypeFilter()
    message = django_filters.CharFilter(lookup_expr='icontains')

    o = django_filters.OrderingFilter(fields=('severity', 'created'))

    class Meta:
        model = models.Alert
        fields = [
            'acknowledged',
            'closed_from',
            'closed_to',
            'created_from',
            'created_to',
            'content_type',
            'message'
        ]


class AlertScopeFilterBackend(core_filters.GenericKeyFilterBackend):

    def get_related_models(self):
        return utils.get_loggable_models()

    def get_field_name(self):
        return 'scope'


class AdditionalAlertFilterBackend(filters.BaseFilterBackend):
    """
    Additional filters for alerts.

    Support for filters that are related to more than one field or provides unusual query.
    """

    def filter_queryset(self, request, queryset, view):
        mapped = {
            'start': request.query_params.get('from'),
            'end': request.query_params.get('to'),
        }
        timestamp_interval_serializer = core_serializers.TimestampIntervalSerializer(
            data={k: v for k, v in mapped.items() if v})
        timestamp_interval_serializer.is_valid(raise_exception=True)
        filter_data = timestamp_interval_serializer.get_filter_data()
        if 'start' in filter_data:
            queryset = queryset.filter(
                Q(closed__gte=filter_data['start']) | Q(closed__isnull=True))
        if 'end' in filter_data:
            queryset = queryset.filter(created__lte=filter_data['end'])

        if 'opened' in request.query_params:
            queryset = queryset.filter(closed__isnull=True)

        if 'closed' in request.query_params:
            queryset = queryset.filter(closed__isnull=False)

        if 'severity' in request.query_params:
            severity_codes = {v: k for k, v in models.Alert.SeverityChoices.CHOICES}
            severities = [
                severity_codes.get(severity_name) for severity_name in request.query_params.getlist('severity')]
            queryset = queryset.filter(severity__in=severities)

        # XXX: this filter is wrong and deprecated, need to be removed after replacement in Portal
        if 'scope_type' in request.query_params:
            choices = {camel_case_to_underscore(m.__name__): m for m in utils.get_loggable_models()}
            try:
                scope_type = choices[request.query_params['scope_type']]
            except KeyError:
                raise ValidationError(
                    _('Scope type "%(value)s" is not valid. Has to be one from list: %(items)s.') % dict(
                        value=request.query_params['scope_type'],
                        items=', '.join(choices.keys())
                    ))
            else:
                ct = ContentType.objects.get_for_model(scope_type)
                queryset = queryset.filter(content_type=ct)

        if 'alert_type' in request.query_params:
            queryset = queryset.filter(alert_type__in=request.query_params.getlist('alert_type'))

        # Group alerts by features in order to prevent large HTTP GET request
        if 'exclude_features' in request.query_params:
            features = request.query_params.getlist('exclude_features')
            queryset = queryset.exclude(alert_type__in=expand_alert_groups(features))

        return queryset


class ExternalAlertFilterBackend(ExternalFilterBackend):
    pass


class BaseHookFilter(django_filters.FilterSet):
    author_uuid = django_filters.UUIDFilter(name='user__uuid')
    is_active = django_filters.BooleanFilter(widget=BooleanWidget)
    last_published = django_filters.DateTimeFilter()


class WebHookFilter(BaseHookFilter):
    class Meta(object):
        model = models.WebHook
        fields = ('destination_url', 'content_type')


class EmailHookFilter(BaseHookFilter):
    class Meta(object):
        model = models.EmailHook
        fields = ('email',)


class HookSummaryFilterBackend(core_filters.SummaryFilter):
    def get_queryset_filter(self, queryset):
        if queryset.model == models.WebHook:
            return WebHookFilter
        elif queryset.model == models.EmailHook:
            return EmailHookFilter
        elif queryset.model == models.PushHook:
            return PushHookFilter

        return BaseHookFilter

    def get_base_filter(self):
        return BaseHookFilter


class PushHookFilter(BaseHookFilter):
    class Meta(object):
        model = models.PushHook
        fields = ('type', 'device_id', 'device_manufacturer', 'device_model', 'token')


class EventFilter(django_filters.FilterSet):
    created_from = core_filters.TimestampFilter(name='created', lookup_expr='gte')
    created_to = core_filters.TimestampFilter(name='created', lookup_expr='lt')
    message = django_filters.CharFilter(lookup_expr='icontains')
    o = django_filters.OrderingFilter(fields=('created',))

    class Meta:
        model = models.Event
        fields = []


class EventFilterBackend(filters.BaseFilterBackend):

    def filter_queryset(self, request, queryset, view):
        event_types = request.query_params.getlist('event_type')
        if event_types:
            queryset = queryset.filter(event_type__in=event_types)

        features = request.query_params.getlist('feature')
        if features:
            queryset = queryset.filter(event_type__in=expand_event_groups(features))

        if 'scope' in request.query_params:
            field = core_serializers.GenericRelatedField(related_models=utils.get_loggable_models())
            field._context = {'request': request}
            scope = field.to_internal_value(request.query_params['scope'])

            # Check permissions
            visible = scope._meta.model.get_permitted_objects(request.user)
            if not visible.filter(pk=scope.pk).exists():
                return queryset.none()

            content_type = ContentType.objects.get_for_model(scope._meta.model)
            events = models.Feed.objects.filter(
                content_type=content_type,
                object_id=scope.id,
            ).values_list('event_id', flat=True)
            queryset = queryset.filter(id__in=events)

        elif not request.user.is_staff and not request.user.is_support:
            # If user is not staff nor support, he is allowed to see
            # events related to particular scope only.
            queryset = queryset.none()

        return queryset
