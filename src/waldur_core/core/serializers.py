from __future__ import unicode_literals

import base64
from collections import OrderedDict
from datetime import timedelta
import logging

from django.core.exceptions import ImproperlyConfigured, MultipleObjectsReturned, ObjectDoesNotExist
from django.urls import reverse, Resolver404
from django.utils.translation import ugettext_lazy as _
from rest_framework import serializers
from rest_framework.fields import Field, ReadOnlyField
from rest_framework.exceptions import ValidationError
import six

from waldur_core.core import utils as core_utils
from waldur_core.core.fields import TimestampField
from waldur_core.core.signals import pre_serializer_fields

logger = logging.getLogger(__name__)


class AuthTokenSerializer(serializers.Serializer):
    """
    API token serializer loosely based on DRF's default AuthTokenSerializer.
    but with the logic of authorization is extracted to view.
    """
    # Fields are both required, non-blank and don't allow nulls by default
    username = serializers.CharField()
    password = serializers.CharField()


class Base64Field(serializers.CharField):
    def to_internal_value(self, data):
        value = super(Base64Field, self).to_internal_value(data)
        try:
            base64.b64decode(value)
            return value
        except (TypeError, ValueError):
            raise serializers.ValidationError(_('This field should a be valid Base64 encoded string.'))

    def to_representation(self, value):
        value = super(Base64Field, self).to_representation(value)
        if isinstance(value, six.text_type):
            value = value.encode('utf-8')
        return base64.b64encode(value)


class BasicInfoSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        fields = ('url', 'uuid', 'name')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class UnboundSerializerMethodField(ReadOnlyField):
    """
    A field that gets its value by calling a provided filter callback.
    """

    def __init__(self, filter_function, *args, **kwargs):
        self.filter_function = filter_function
        super(UnboundSerializerMethodField, self).__init__(*args, **kwargs)

    def to_representation(self, value):
        request = self.context.get('request')
        return self.filter_function(value, request)


class GenericRelatedField(Field):
    """
    A custom field to use for the `tagged_object` generic relationship.
    """
    read_only = False
    _default_view_name = '%(model_name)s-detail'
    lookup_fields = ['uuid', 'pk']

    def __init__(self, related_models=(), **kwargs):
        super(GenericRelatedField, self).__init__(**kwargs)
        self._related_models = related_models

    @property
    def related_models(self):
        val = self._related_models
        return callable(val) and val() or val

    def _get_url(self, obj):
        """
        Gets object url
        """
        format_kwargs = {
            'app_label': obj._meta.app_label,
        }
        try:
            format_kwargs['model_name'] = getattr(obj.__class__, 'get_url_name')()
        except AttributeError:
            format_kwargs['model_name'] = obj._meta.object_name.lower()
        return self._default_view_name % format_kwargs

    def _get_request(self):
        try:
            return self.context['request']
        except KeyError:
            raise AttributeError('GenericRelatedField have to be initialized with `request` in context')

    def to_representation(self, obj):
        """
        Serializes any object to his url representation
        """
        kwargs = None
        for field in self.lookup_fields:
            if hasattr(obj, field):
                kwargs = {field: getattr(obj, field)}
                break
        if kwargs is None:
            raise AttributeError('Related object does not have any of lookup_fields')
        request = self._get_request()
        return request.build_absolute_uri(reverse(self._get_url(obj), kwargs=kwargs))

    def to_internal_value(self, data):
        """
        Restores model instance from its url
        """
        if not data:
            return None
        request = self._get_request()
        user = request.user
        try:
            obj = core_utils.instance_from_url(data, user=user)
            model = obj.__class__
        except ValueError:
            raise serializers.ValidationError(_('URL is invalid: %s.') % data)
        except (Resolver404, AttributeError, MultipleObjectsReturned, ObjectDoesNotExist):
            raise serializers.ValidationError(_("Can't restore object from url: %s") % data)

        if model not in self.related_models:
            context = (model, ', '.join(six.text_type(model) for model in self.related_models))
            message = _('%s is not valid. Valid models are: %s') % context
            raise serializers.ValidationError(message)

        return obj


class AugmentedSerializerMixin(object):
    """
    This mixin provides several extensions to stock Serializer class:

    1.  Add extra fields to serializer from dependent applications in a way
        that doesn't introduce circular dependencies.

        To achieve this, dependent application should subscribe
        to pre_serializer_fields signal and inject additional fields.

        Example of signal handler implementation:

        from waldur_core.structure.serializers import CustomerSerializer

        def add_customer_name(sender, fields, **kwargs):
            fields['customer_name'] = ReadOnlyField(source='customer.name')

        pre_serializer_fields.connect(
            handlers.add_customer_name,
            sender=CustomerSerializer
        )

    2.  Declaratively add attributes fields of related entities for ModelSerializers.

        To achieve list related fields whose attributes you want to include.

        Example:
            class ProjectSerializer(AugmentedSerializerMixin,
                                    serializers.HyperlinkedModelSerializer):
                class Meta(object):
                    model = models.Project
                    fields = (
                        'url', 'uuid', 'name',
                        'customer', 'customer_uuid', 'customer_name',
                    )
                    related_paths = ('customer',)

            # This is equivalent to listing the fields explicitly,
            # by default "uuid" and "name" fields of related object are added:

            class ProjectSerializer(AugmentedSerializerMixin,
                                    serializers.HyperlinkedModelSerializer):
                customer_uuid = serializers.ReadOnlyField(source='customer.uuid')
                customer_name = serializers.ReadOnlyField(source='customer.name')
                class Meta(object):
                    model = models.Project
                    fields = (
                        'url', 'uuid', 'name',
                        'customer', 'customer_uuid', 'customer_name',
                    )
                    lookup_field = 'uuid'

            # The fields of related object can be customized:

            class ProjectSerializer(AugmentedSerializerMixin,
                                    serializers.HyperlinkedModelSerializer):
                class Meta(object):
                    model = models.Project
                    fields = (
                        'url', 'uuid', 'name',
                        'customer', 'customer_uuid',
                        'customer_name', 'customer_native_name',
                    )
                    related_paths = {
                        'customer': ('uuid', 'name', 'native_name')
                    }

    3.  Protect some fields from change.

        Example:
            class ProjectSerializer(AugmentedSerializerMixin,
                                    serializers.HyperlinkedModelSerializer):
                class Meta(object):
                    model = models.Project
                    fields = ('url', 'uuid', 'name', 'customer')
                    protected_fields = ('customer',)

    4. This mixin overrides "get_extra_kwargs" method and puts "view_name" to extra_kwargs
    or uses URL name specified in a model of serialized object.
    """

    def get_fields(self):
        fields = super(AugmentedSerializerMixin, self).get_fields()
        pre_serializer_fields.send(sender=self.__class__, fields=fields)

        try:
            protected_fields = self.Meta.protected_fields
        except AttributeError:
            pass
        else:
            try:
                method = self.context['view'].request.method
            except (KeyError, AttributeError):
                return fields

            if method in ('PUT', 'PATCH'):
                for field in protected_fields:
                    fields[field].read_only = True

        return fields

    def _get_related_paths(self):
        try:
            related_paths = self.Meta.related_paths
        except AttributeError:
            return {}

        if not isinstance(self, serializers.ModelSerializer):
            raise ImproperlyConfigured(
                'related_paths can be defined only for ModelSerializer.'
            )

        if isinstance(related_paths, (list, tuple)):
            related_paths = {path: ('name', 'uuid') for path in related_paths}

        return related_paths

    def build_unknown_field(self, field_name, model_class):
        related_paths = self._get_related_paths()

        related_field_source_map = {
            '{0}_{1}'.format(path.split('.')[-1], attribute): '{0}.{1}'.format(path, attribute)
            for path, attributes in related_paths.items()
            for attribute in attributes
        }

        try:
            return serializers.ReadOnlyField, {'source': related_field_source_map[field_name]}
        except KeyError:
            return super(AugmentedSerializerMixin, self).build_unknown_field(field_name, model_class)

    def get_extra_kwargs(self):
        extra_kwargs = super(AugmentedSerializerMixin, self).get_extra_kwargs()

        if hasattr(self.Meta, 'view_name'):
            view_name = self.Meta.view_name
        else:
            view_name = core_utils.get_detail_view_name(self.Meta.model)

        if 'url' in extra_kwargs:
            extra_kwargs['url']['view_name'] = view_name
        else:
            extra_kwargs['url'] = {'view_name': view_name}

        return extra_kwargs


class RestrictedSerializerMixin(object):
    """
    This mixin allows to specify list of fields to be rendered by serializer.
    It expects that request is available in serializer's context.
    """

    FIELDS_PARAM_NAME = 'field'

    def get_fields(self):
        fields = super(RestrictedSerializerMixin, self).get_fields()
        if 'request' not in self.context:
            return fields
        query_params = self.context['request'].query_params
        keys = query_params.getlist(self.FIELDS_PARAM_NAME)
        keys = set(key for key in keys if key in fields.keys())
        if not keys:
            return fields
        return OrderedDict(((key, value) for key, value in fields.items() if key in keys))


class RequiredFieldsMixin(object):
    """
    This mixin allows to specify list of required fields.
    It expects list of field names as Meta.required_fields attribute.
    """

    def get_fields(self):
        fields = super(RequiredFieldsMixin, self).get_fields()
        required_fields = getattr(self.Meta, 'required_fields') or []
        for name in required_fields:
            field = fields.get(name)
            if field:
                field.required = True
        return fields


class ExtraFieldOptionsMixin(object):
    """
    This mixin allows to specify extra fields metadata.
    It expects dictionary of field name and options as Meta.extra_field_options attribute.
    """

    def get_fields(self):
        fields = super(ExtraFieldOptionsMixin, self).get_fields()
        extra_field_options = getattr(self.Meta, 'extra_field_options') or {}
        for name, options in extra_field_options.items():
            field = fields.get(name)
            if field:
                for key, val in options.items():
                    setattr(field, key, val)
        return fields


class HyperlinkedRelatedModelSerializer(serializers.HyperlinkedModelSerializer):
    def __init__(self, **kwargs):
        self.queryset = kwargs.pop('queryset', None)
        assert self.queryset is not None or kwargs.get('read_only', None), (
            'Relational field must provide a `queryset` argument, '
            'or set read_only=`True`.'
        )
        assert not (self.queryset is not None and kwargs.get('read_only', None)), (
            'Relational fields should not provide a `queryset` argument, '
            'when setting read_only=`True`.'
        )
        super(HyperlinkedRelatedModelSerializer, self).__init__(**kwargs)

    def to_internal_value(self, data):
        if 'url' not in data:
            raise serializers.ValidationError(_('URL has to be defined for related object.'))
        url_field = self.fields['url']

        # This is tricky: self.fields['url'] is the one generated
        # based on Meta.fields.
        # By default ModelSerializer generates it as HyperlinkedIdentityField,
        # which is read-only, thus it doesn't get deserialized from POST body.
        # So, we "borrow" its view_name and lookup_field to create
        # a HyperlinkedRelatedField which can turn url into a proper model
        # instance.
        url = serializers.HyperlinkedRelatedField(
            queryset=self.queryset.all(),
            view_name=url_field.view_name,
            lookup_field=url_field.lookup_field,
        )

        return url.to_internal_value(data['url'])


class TimestampIntervalSerializer(serializers.Serializer):
    start = TimestampField(required=False)
    end = TimestampField(required=False)

    def validate(self, data):
        """
        Check that the start is before the end.
        """
        if 'start' in data and 'end' in data and data['start'] >= data['end']:
            raise serializers.ValidationError(_('End must occur after start.'))
        return data

    # TimeInterval serializer is used for validation only. We are providing custom method for such serializers
    # to avoid confusion with to_internal_value or to_representation DRF methods.
    def get_filter_data(self):
        """ Return start and end as datetime """
        return self.validated_data


class HistorySerializer(serializers.Serializer):
    """
    Receive datetime as timestamps and converts them to list of datetimes

    Support 2 types of input data:
     - start, end and points_count - interval from <start> to <end> will be automatically split into
                                     <points_count> pieces
     - point_list - list of timestamps that will be converted to datetime points

    """
    start = TimestampField(required=False)
    end = TimestampField(required=False)
    points_count = serializers.IntegerField(min_value=2, required=False)
    point_list = serializers.ListField(
        child=TimestampField(),
        required=False
    )

    def validate(self, attrs):
        autosplit_fields = {'start', 'end', 'points_count'}
        if ('point_list' not in attrs or not attrs['point_list']) and not autosplit_fields == set(attrs.keys()):
            raise serializers.ValidationError(
                _('Not enough parameters for historical data. '
                  '(Either "point" or "start" + "end" + "points_count" parameters have to be provided).'))
        if 'point_list' in attrs and autosplit_fields & set(attrs.keys()):
            raise serializers.ValidationError(
                _('Too many parameters for historical data. '
                  '(Either "point" or "start" + "end" + "points_count" parameters have to be provided).'))
        if 'point_list' not in attrs and not attrs['start'] < attrs['end']:
            raise serializers.ValidationError(_('Start timestamps have to be later than end timestamps.'))
        return attrs

    # History serializer is used for validation only. We are providing custom method for such serializers
    # to avoid confusion with to_internal_value or to_representation DRF methods.
    def get_filter_data(self):
        if 'point_list' in self.validated_data:
            return self.validated_data['point_list']
        else:
            interval = ((self.validated_data['end'] - self.validated_data['start']) /
                        (self.validated_data['points_count'] - 1))
            return [self.validated_data['start'] + interval * i for i in range(self.validated_data['points_count'])]


class TimelineSerializer(serializers.Serializer):

    INTERVAL_CHOICES = ('hour', 'day', 'week', 'month')

    start_time = TimestampField(default=lambda: core_utils.timeshift(days=-1))
    end_time = TimestampField(default=lambda: core_utils.timeshift())
    interval = serializers.ChoiceField(choices=INTERVAL_CHOICES, default='day')

    def get_date_points(self):
        start_time = self.validated_data['start_time']
        end_time = self.validated_data['end_time']
        interval = self.validated_data['interval']

        if interval == 'hour':
            start_point = start_time.replace(second=0, minute=0, microsecond=0)
            interval = timedelta(hours=1)
        elif interval == 'day':
            start_point = start_time.replace(hour=0, second=0, minute=0, microsecond=0)
            interval = timedelta(days=1)
        elif interval == 'week':
            start_point = start_time.replace(hour=0, second=0, minute=0, microsecond=0)
            interval = timedelta(days=7)
        elif interval == 'month':
            start_point = start_time.replace(hour=0, second=0, minute=0, microsecond=0)
            interval = timedelta(days=30)

        points = [start_time]
        current_point = start_point
        while current_point <= end_time:
            points.append(current_point)
            current_point += interval
        if points[-1] != end_time:
            points.append(end_time)

        return [p for p in points if start_time <= p <= end_time]


class BaseSummarySerializer(serializers.Serializer):
    """ Serializer that renders each instance with its own specific serializer """

    @classmethod
    def get_serializer(cls, model):
        raise NotImplementedError('Method `get_serializer` should be implemented for SummarySerializer.')

    @classmethod
    def eager_load(cls, summary_queryset, request):
        optimized_querysets = []
        for queryset in summary_queryset.querysets:
            serializer = cls.get_serializer(queryset.model)
            optimized_querysets.append(serializer.eager_load(queryset, request))
        summary_queryset.querysets = optimized_querysets
        return summary_queryset

    def to_representation(self, instance):
        serializer = self.get_serializer(instance.__class__)
        return serializer(instance, context=self.context).data


class GeoLocationField(serializers.JSONField):
    def __init__(self, *args, **kwargs):
        validators = kwargs.get('validators', [])

        def geo_location_validator(value):
            if value is not None:
                if not isinstance(value, list):
                    raise ValidationError(_('GeoLocationField should be a list of dictionaries.'))
                else:
                    for location in value:
                        if not isinstance(location, dict):
                            raise ValidationError(_('GeoLocationField should be a list of dictionaries.'))
                        if not {'latitude', 'longitude'}.issubset(location.keys()):
                            raise ValidationError(_('GeoLocationField should be a list of dictionaries. For example: '
                                                    '[{"latitude": 123, "longitude": 345}, '
                                                    '{"latitude": 456, "longitude": 678}]'))
            return value

        validators.append(geo_location_validator)
        super(GeoLocationField, self).__init__(validators=validators, *args, **kwargs)
