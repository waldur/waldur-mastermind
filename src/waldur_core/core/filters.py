from urllib.parse import urlparse

import django_filters
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.forms.fields import MultipleChoiceField
from django.urls import resolve
from django_filters.constants import EMPTY_VALUES
from django_filters.filters import MultipleChoiceFilter
from rest_framework.filters import BaseFilterBackend

from waldur_core.core import fields as core_fields
from waldur_core.core import mixins as core_mixins
from waldur_core.core import serializers as core_serializers
from waldur_core.core import utils as core_utils


class GenericKeyFilterBackend(BaseFilterBackend):
    """
    Backend for filtering by backend field.

    Methods 'get_related_models' and 'get_field_name' has to be implemented.
    Example:

        class ScopeFilterBackend(core_filters.GenericKeyFilterBackend):

            def get_related_models(self):
                return utils.get_loggable_models()

            def get_field_name(self):
                return 'scope'
    """

    content_type_field = "content_type"
    object_id_field = "object_id"

    def is_anonymous_allowed(self):
        return False

    def get_related_models(self):
        """Return all models that are acceptable as filter argument"""
        raise NotImplementedError

    def get_field_name(self):
        """Get name of filter field name in request"""
        raise NotImplementedError

    def get_field_value(self, request):
        field_name = self.get_field_name()
        return request.query_params.get(field_name)

    def filter_queryset(self, request, queryset, view):
        value = self.get_field_value(request)
        if value:
            field = core_serializers.GenericRelatedField(
                related_models=self.get_related_models()
            )
            # Trick to set field context without serializer
            field._context = {"request": request}
            if self.is_anonymous_allowed() and request.user.is_anonymous:
                request.user = None
            obj = field.to_internal_value(value)
            ct = ContentType.objects.get_for_model(obj)
            return queryset.filter(
                **{self.object_id_field: obj.id, self.content_type_field: ct}
            )
        return queryset


class MappedMultipleChoiceFilter(django_filters.MultipleChoiceFilter):
    """
    A multiple choice field that maps enum values from representation to model ones and back.

    Filter analog for MappedChoiceField that allow to filter by several choices.
    """

    def __init__(self, choices, **kwargs):
        super().__init__(
            **kwargs,
            choices=[
                (representation, representation) for db_value, representation in choices
            ],
        )

        self.choice_mappings = {
            representation: db_value for db_value, representation in choices
        }

    def filter(self, qs, value):
        value = [self.choice_mappings[v] for v in value if v in self.choice_mappings]
        return super().filter(qs, value)


class LooseMultipleChoiceField(MultipleChoiceField):
    def valid_value(self, value):
        return True


class LooseMultipleChoiceFilter(MultipleChoiceFilter):
    """
    A multiple choice filter field that skips validation of values.
    Based on https://github.com/carltongibson/django-filter/issues/137#issuecomment-37820702
    """

    field_class = LooseMultipleChoiceField


class URLFilter(django_filters.CharFilter):
    """Filter by hyperlinks. ViewSet name must be supplied in order to validate URL."""

    def __init__(self, view_name, lookup_field="uuid", **kwargs):
        super().__init__(**kwargs)
        self.view_name = view_name
        self.lookup_field = lookup_field

    def get_uuid(self, value):
        uuid_value = ""
        path = urlparse(value).path
        if path.startswith("/"):
            match = resolve(path)
            if match.url_name == self.view_name:
                uuid_value = match.kwargs.get(self.lookup_field)
        return uuid_value

    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs

        uuid_value = self.get_uuid(value)
        if not core_utils.is_uuid_like(uuid_value):
            return qs.none()
        return super().filter(qs, uuid_value)


class TimestampFilter(django_filters.NumberFilter):
    """
    Filter for dates in timestamp format
    """

    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs

        field = core_fields.TimestampField()
        datetime_value = field.to_internal_value(value)
        return super().filter(qs, datetime_value)


class CategoryFilter(django_filters.CharFilter):
    """
    Filters queryset by category names.
    If category name does not match, it will work as CharFilter.

    :param categories: dictionary of category names as keys and category elements as values.
    """

    def __init__(self, categories, **kwargs):
        super().__init__(**kwargs)
        self.categories = categories

    def filter(self, qs, value):
        if value in self.categories.keys():
            return qs.filter(**{"%s__in" % self.name: self.categories[value]})

        return super().filter(qs, value)


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
            app_label, model = value.split(".")
            ct = ContentType.objects.get(app_label=app_label, model=model)
            return super().filter(qs, ct)
        except (ContentType.DoesNotExist, ValueError):
            return qs.none()


class ExternalFilterBackend(BaseFilterBackend):
    """
    Support external filters registered in other apps
    """

    @classmethod
    def get_registered_filters(cls):
        return getattr(cls, "_filters", [])

    @classmethod
    def register(cls, external_filter):
        assert isinstance(
            external_filter, BaseFilterBackend
        ), "Registered filter has to inherit BaseFilterBackend"
        if hasattr(cls, "_filters"):
            cls._filters.append(external_filter)
        else:
            cls._filters = [external_filter]

    def filter_queryset(self, request, queryset, view):
        for item in self.__class__.get_registered_filters():
            queryset = item.filter_queryset(request, queryset, view)
        return queryset


class SummaryFilter(BaseFilterBackend):
    """Base filter for summary querysets"""

    def filter_queryset(self, request, queryset, view):
        queryset = self.filter(request, queryset, view)
        return queryset

    def get_queryset_filter(self, queryset):
        """Return specific for queryset filter if it exists"""
        raise NotImplementedError()

    def get_base_filter(self):
        """Return base filter that could be used for all summary objects"""
        raise NotImplementedError()

    def _get_filter(self, queryset):
        try:
            return self.get_queryset_filter(queryset)
        except NotImplementedError:
            return self.get_base_filter()

    def filter(self, request, queryset, view):
        """Filter each resource separately using its own filter"""
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


class ExtendedOrderingFilter(django_filters.OrderingFilter):
    """This filter allows to use list or tuple of model fields in defining of ordering fields in filter.

    For example:

    class MyFilterSet(django_filters.FilterSet):
        o = core_filters.ExtendedOrderingFilter(
            fields=(
                ('created', 'created'),
                (('first_name', 'last_name'), 'full_name'),
            )
        )
    """

    def get_ordering_value(self, param):
        descending = param.startswith("-")
        param = param[1:] if descending else param
        field_name = self.param_map.get(param, param)

        if not isinstance(field_name, tuple | list):
            field_name = [field_name]

        return list(map(lambda x: "-%s" % x if descending else x, field_name))

    def filter(self, qs, value):
        if value in EMPTY_VALUES:
            return qs

        ordering = []

        for param_name in value:
            param = self.get_ordering_value(param_name)
            ordering += param

        return qs.order_by(*ordering)


class CreatedModifiedFilter(django_filters.FilterSet):
    created = django_filters.DateFilter(lookup_expr="gte", label="Created after")
    modified = django_filters.DateFilter(lookup_expr="gte", label="Modified after")


def filter_by_full_name(queryset, value, field=""):
    field = field and field + "__"
    return queryset.filter(
        **{field + "query_field__icontains": core_utils.normalize_unicode(value)}
    )


def filter_by_user_keyword(queryset, value):
    return queryset.filter(
        Q(**{"query_field__icontains": core_utils.normalize_unicode(value)})
        | Q(email__icontains=value)
        | Q(username__icontains=value)
    ).distinct()


class ReviewStateFilter(MappedMultipleChoiceFilter):
    def __init__(self, *args, **kwargs):
        kwargs["choices"] = core_mixins.ReviewMixin.States.CHOICES
        super().__init__(*args, **kwargs)


def get_generic_field_filter(get_related_models: list):
    def generic_field_filter(queryset, name, value):
        for klass in get_related_models:
            if klass.objects.filter(uuid=value).exists():
                obj = klass.objects.get(uuid=value)
                ct = ContentType.objects.get_for_model(klass)
                return queryset.filter(object_id=obj.id, content_type=ct)
        return queryset.none()

    return generic_field_filter
