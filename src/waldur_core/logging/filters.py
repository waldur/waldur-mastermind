from __future__ import unicode_literals

from django.conf import settings as django_settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
import django_filters
from django_filters.widgets import BooleanWidget
from rest_framework import settings, filters
from rest_framework.serializers import ValidationError

from waldur_core.core import serializers as core_serializers, filters as core_filters
from waldur_core.core.filters import ExternalFilterBackend
from waldur_core.core.utils import camel_case_to_underscore, get_ordering
from waldur_core.logging import models, utils
from waldur_core.logging.elasticsearch_client import EmptyQueryset
from waldur_core.logging.loggers import event_logger, expand_event_groups, expand_alert_groups


def format_raw_field(key):
    """
    When ElasticSearch analyzes string, it breaks it into parts.
    In order make query for not-analyzed exact string values, we should use subfield instead.

    The index template for Elasticsearch 5.0 has been changed.
    The subfield for string multi-fields has changed from .raw to .keyword

    Thus workaround for backward compatibility during migration is required.
    See also: https://github.com/elastic/logstash/blob/v5.4.1/docs/static/breaking-changes.asciidoc
    """
    subfield = django_settings.WALDUR_CORE.get('ELASTICSEARCH', {}).get('raw_subfield', 'keyword')
    return '%s.%s' % (camel_case_to_underscore(key), subfield)


class EventFilterBackend(filters.BaseFilterBackend):
    """ Sorting is supported in ascending and descending order by specifying a field to
        an **?o=** parameter. By default events are sorted by @timestamp in descending order.

        - ?o=\@timestamp

        Filtering of customer list is supported through HTTP query parameters, the following fields are supported:

        - ?event_type=<string> - type of filtered events. Can be list
        - ?search=<string> - text for FTS. FTS fields: 'message', 'customer_abbreviation', 'importance'
          'project_name', 'user_full_name', 'user_native_name'
        - ?scope=<URL> - url of object that is connected to event
        - ?scope_type=<string> - name of scope type of object that is connected to event
        - ?feature=<feature> (can be list) - include all event with type that belong to given features
        - ?exclude_features=<feature> (can be list) - exclude event from output if
          it's type corresponds to one of listed features
        - ?user_username=<string> - user's username
        - ?from=<timestamp> - beginning UNIX timestamp
        - ?to=<timestamp> - ending UNIX timestamp
    """

    def filter_queryset(self, request, queryset, view):
        search_text = request.query_params.get(settings.api_settings.SEARCH_PARAM, '')
        must_terms = {}
        must_not_terms = {}
        should_terms = {}
        excluded_event_types = set()

        if 'event_type' in request.query_params:
            must_terms['event_type'] = request.query_params.getlist('event_type')

        if 'feature' in request.query_params:
            features = request.query_params.getlist('feature')
            must_terms['event_type'] = expand_event_groups(features)

        # Group events by features in order to prevent large HTTP GET request
        if 'exclude_features' in request.query_params:
            features = request.query_params.getlist('exclude_features')
            excluded_event_types.update(expand_event_groups(features))

        if 'exclude_extra' in request.query_params:
            excluded_event_types.update(expand_event_groups(['update']))

        if not django_settings.DEBUG:
            excluded_event_types.update(expand_event_groups(['debug_only']))

        if excluded_event_types:
            must_not_terms['event_type'] = list(excluded_event_types)

        if 'user_username' in request.query_params:
            must_terms['user_username'] = [request.query_params.get('user_username')]

        if 'scope' in request.query_params:
            field = core_serializers.GenericRelatedField(related_models=utils.get_loggable_models())
            field._context = {'request': request}
            obj = field.to_internal_value(request.query_params['scope'])

            # XXX: Ilja - disabling this hack and re-opening a ticket. Additional analysis is required for
            # a proper resolution
            # # XXX: hack to prevent leaking customer events
            # permitted_uuids = [uuid.hex for uuids in
            #                    obj.get_permitted_objects_uuids(request.user).values() for uuid in uuids]
            # if obj.uuid.hex not in permitted_uuids:
            #     raise ValidationError('You do not have permission to view events for scope %s'
            #                           % request.query_params['scope'])

            for key, val in obj.filter_by_logged_object().items():
                must_terms[format_raw_field(key)] = [val]

        elif 'scope_type' in request.query_params:
            choices = utils.get_scope_types_mapping()
            try:
                scope_type = choices[request.query_params['scope_type']]
            except KeyError:
                raise ValidationError(
                    _('Scope type "%(value)s" is not valid. Has to be one from list: %(items)s.') % dict(
                        value=request.query_params['scope_type'],
                        items=', '.join(choices.keys())
                    ))
            else:
                permitted_items = scope_type.get_permitted_objects_uuids(request.user).items()
                if not permitted_items:
                    return EmptyQueryset()
                for field, uuids in permitted_items:
                    must_terms[field] = [uuid.hex for uuid in uuids]

        elif 'resource_type' in request.query_params and 'resource_uuid' in request.query_params:
            # Filter events by resource type and uuid.
            # Please note, that permission checks are skipped,
            # because we can't check permission for deleted resources.
            # Also note, that resource type validation is skipped as well,
            # because resource type name formatting is defined in structure application,
            # but we don't want to create circular dependency between logging and structure apps.
            # This issue could be fixed by switching resource type name formatting to str(model._meta)
            # as it is done for scope_type parameter validation.
            must_terms[format_raw_field('resource_type')] = [request.query_params['resource_type']]
            must_terms[format_raw_field('resource_uuid')] = [request.query_params['resource_uuid']]

        else:
            should_terms.update(event_logger.get_permitted_objects_uuids(request.user))

        mapped = {
            'start': request.query_params.get('from'),
            'end': request.query_params.get('to'),
        }
        timestamp_interval_serializer = core_serializers.TimestampIntervalSerializer(
            data={k: v for k, v in mapped.items() if v})
        timestamp_interval_serializer.is_valid(raise_exception=True)
        filter_data = timestamp_interval_serializer.get_filter_data()

        queryset = queryset.filter(search_text=search_text,
                                   should_terms=should_terms,
                                   must_terms=must_terms,
                                   must_not_terms=must_not_terms,
                                   start=filter_data.get('start'),
                                   end=filter_data.get('end'))

        order_by = get_ordering(request) or '-@timestamp'
        if order_by:
            queryset = queryset.order_by(order_by)

        return queryset


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
