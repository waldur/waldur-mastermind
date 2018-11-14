import uuid

from django.contrib.contenttypes.models import ContentType
from django.forms.fields import MultipleChoiceField
from django.urls import resolve
import django_filters
from django_filters.constants import EMPTY_VALUES
from django_filters.filters import MultipleChoiceFilter
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import BaseFilterBackend
import six
from six.moves.urllib.parse import urlparse

from waldur_core.core import serializers as core_serializers, fields as core_fields, models as core_models


class GenericKeyFilterBackend(DjangoFilterBackend):
    """
    Backend for filtering by backend field.

    Methods 'get_related_models' and 'get_field_name' has to be implemented.
    Example:

        class AlertScopeFilterBackend(core_filters.GenericKeyFilterBackend):

            def get_related_models(self):
                return utils.get_loggable_models()

            def get_field_name(self):
                return 'scope'
    """
    content_type_field = 'content_type'
    object_id_field = 'object_id'

    def get_related_models(self):
        """ Return all models that are acceptable as filter argument """
        raise NotImplementedError

    def get_field_name(self):
        """ Get name of filter field name in request """
        raise NotImplementedError

    def get_field_value(self, request):
        field_name = self.get_field_name()
        return request.query_params.get(field_name)

    def filter_queryset(self, request, queryset, view):
        value = self.get_field_value(request)
        if value:
            field = core_serializers.GenericRelatedField(related_models=self.get_related_models())
            # Trick to set field context without serializer
            field._context = {'request': request}
            obj = field.to_internal_value(value)
            ct = ContentType.objects.get_for_model(obj)
            return queryset.filter(**{self.object_id_field: obj.id, self.content_type_field: ct})
        return queryset


class MappedFilterMixin(object):

    def __init__(self, choice_mappings, **kwargs):
        super(MappedFilterMixin, self).__init__(**kwargs)

        # TODO: enable this assert then filtering by numbers will be disabled
        # assert set(k for k, _ in self.field.choices) == set(choice_mappings.keys()), 'Choices do not match mappings'
        assert len(set(choice_mappings.values())) == len(choice_mappings), 'Mappings are not unique'

        self.mapped_to_model = choice_mappings
        self.model_to_mapped = {v: k for k, v in six.iteritems(choice_mappings)}


class MappedChoiceFilter(MappedFilterMixin, django_filters.ChoiceFilter):
    """
    A choice field that maps enum values from representation to model ones and back.

    Filter analog for MappedChoiceField.
    """

    def filter(self, qs, value):
        if value in self.mapped_to_model:
            value = self.mapped_to_model[value]
        return super(MappedChoiceFilter, self).filter(qs, value)


class MappedMultipleChoiceFilter(MappedFilterMixin, django_filters.MultipleChoiceFilter):
    """
    A multiple choice field that maps enum values from representation to model ones and back.

    Filter analog for MappedChoiceField that allow to filter by several choices.
    """

    def filter(self, qs, value):
        value = [self.mapped_to_model[v] for v in value if v in self.mapped_to_model]
        return super(MappedMultipleChoiceFilter, self).filter(qs, value)


class LooseMultipleChoiceField(MultipleChoiceField):
    def valid_value(self, value):
        return True


class LooseMultipleChoiceFilter(MultipleChoiceFilter):
    """
    A multiple choice filter field that skips validation of values.
    Based on https://github.com/carltongibson/django-filter/issues/137#issuecomment-37820702
    """
    field_class = LooseMultipleChoiceField


class StateFilter(MappedMultipleChoiceFilter):

    DEFAULT_CHOICES = (
        ('Creation Scheduled', 'Creation Scheduled'),
        ('Creating', 'Creating'),
        ('Update Scheduled', 'Update Scheduled'),
        ('Updating', 'Updating'),
        ('Deletion Scheduled', 'Deletion Scheduled'),
        ('Deleting', 'Deleting'),
        ('OK', 'OK'),
        ('Erred', 'Erred'),
    )

    States = core_models.StateMixin.States
    DEFAULT_CHOICE_MAPPING = {
        'Creation Scheduled': States.CREATION_SCHEDULED,
        'Creating': States.CREATING,
        'Update Scheduled': States.UPDATE_SCHEDULED,
        'Updating': States.UPDATING,
        'Deletion Scheduled': States.DELETION_SCHEDULED,
        'Deleting': States.DELETING,
        'OK': States.OK,
        'Erred': States.ERRED,
    }

    def __init__(self, choices=DEFAULT_CHOICES, choice_mappings=None, **kwargs):
        if choice_mappings is None:
            choice_mappings = self.DEFAULT_CHOICE_MAPPING
        super(StateFilter, self).__init__(choices=choices, choice_mappings=choice_mappings, **kwargs)


class URLFilter(django_filters.CharFilter):
    """ Filter by hyperlinks. ViewSet name must be supplied in order to validate URL. """

    def __init__(self, view_name, lookup_field='uuid', **kwargs):
        super(URLFilter, self).__init__(**kwargs)
        self.view_name = view_name
        self.lookup_field = lookup_field

    def get_uuid(self, value):
        uuid_value = ''
        path = urlparse(value).path
        if path.startswith('/'):
            match = resolve(path)
            if match.url_name == self.view_name:
                uuid_value = match.kwargs.get(self.lookup_field)
        return uuid_value

    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs

        uuid_value = self.get_uuid(value)
        try:
            uuid.UUID(uuid_value)
        except ValueError:
            return qs.none()
        return super(URLFilter, self).filter(qs, uuid_value)


class TimestampFilter(django_filters.NumberFilter):
    """
    Filter for dates in timestamp format
    """

    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs

        field = core_fields.TimestampField()
        datetime_value = field.to_internal_value(value)
        return super(TimestampFilter, self).filter(qs, datetime_value)


class CategoryFilter(django_filters.CharFilter):
    """
    Filters queryset by category names.
    If category name does not match, it will work as CharFilter.

    :param categories: dictionary of category names as keys and category elements as values.
    """

    def __init__(self, categories, **kwargs):
        super(CategoryFilter, self).__init__(**kwargs)
        self.categories = categories

    def filter(self, qs, value):
        if value in self.categories.keys():
            return qs.filter(**{'%s__in' % self.name: self.categories[value]})

        return super(CategoryFilter, self).filter(qs, value)


class StaffOrUserFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        if request.user.is_staff or request.user.is_support:
            return queryset

        return queryset.filter(user=request.user)


class ContentTypeFilter(django_filters.CharFilter):

    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs
        try:
            app_label, model = value.split('.')
            ct = ContentType.objects.get(app_label=app_label, model=model)
            return super(ContentTypeFilter, self).filter(qs, ct)
        except (ContentType.DoesNotExist, ValueError):
            return qs.none()


class BaseExternalFilter(object):
    """ Interface for external alert filter """

    def filter(self, request, queryset, view):
        raise NotImplementedError


class ExternalFilterBackend(BaseFilterBackend):
    """
    Support external filters registered in other apps
    """

    @classmethod
    def get_registered_filters(cls):
        return getattr(cls, '_filters', [])

    @classmethod
    def register(cls, external_filter):
        assert isinstance(external_filter, BaseExternalFilter), 'Registered filter has to inherit BaseExternalFilter'
        if hasattr(cls, '_filters'):
            cls._filters.append(external_filter)
        else:
            cls._filters = [external_filter]

    def filter_queryset(self, request, queryset, view):
        for filt in self.__class__.get_registered_filters():
            queryset = filt.filter(request, queryset, view)
        return queryset


class SummaryFilter(DjangoFilterBackend):
    """ Base filter for summary querysets """

    def filter_queryset(self, request, queryset, view):
        queryset = self.filter(request, queryset, view)
        return queryset

    def get_queryset_filter(self, queryset):
        """ Return specific for queryset filter if it exists """
        raise NotImplementedError()

    def get_base_filter(self):
        """ Return base filter that could be used for all summary objects """
        raise NotImplementedError()

    def _get_filter(self, queryset):
        try:
            return self.get_queryset_filter(queryset)
        except NotImplementedError:
            return self.get_base_filter()

    def filter(self, request, queryset, view):
        """ Filter each resource separately using its own filter """
        summary_queryset = queryset
        filtered_querysets = []
        for queryset in summary_queryset.querysets:
            filter_class = self._get_filter(queryset)
            queryset = filter_class(request.query_params, queryset=queryset).qs
            filtered_querysets.append(queryset)

        summary_queryset.querysets = filtered_querysets
        return summary_queryset


class EmptyFilter(django_filters.CharFilter):
    """
    This filter always returns empty queryset for non-empty value.
    It is used when model does not support particular filter field yet it
    should not simply ignore unknown field and instead should return empty queryset.
    """
    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs
        else:
            return qs.none()
