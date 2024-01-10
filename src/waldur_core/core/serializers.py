import base64
import logging
import re
from collections import OrderedDict
from os.path import join

from constance import LazyConfig, settings
from django.conf import settings as django_settings
from django.core.exceptions import (
    ImproperlyConfigured,
    MultipleObjectsReturned,
    ObjectDoesNotExist,
)
from django.core.files.storage import default_storage
from django.core.validators import RegexValidator, URLValidator
from django.urls import Resolver404, reverse
from django.utils.translation import gettext_lazy as _
from modeltranslation.manager import get_translatable_fields_for_model
from rest_framework import serializers
from rest_framework.fields import Field, ReadOnlyField

from waldur_core.core import utils as core_utils
from waldur_core.core.signals import pre_serializer_fields

from . import fields as core_fields

logger = logging.getLogger(__name__)
config = LazyConfig()


class AuthTokenSerializer(serializers.Serializer):
    """
    API token serializer loosely based on DRF's default AuthTokenSerializer.
    but with the logic of authorization is extracted to view.
    """

    # Fields are both required, non-blank and don't allow nulls by default
    username = serializers.CharField(max_length=128)
    password = serializers.CharField(max_length=128)


class Base64Field(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            base64.b64decode(value)
            return value
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                _("This field should a be valid Base64 encoded string.")
            )

    def to_representation(self, value):
        value = super().to_representation(value)
        if isinstance(value, str):
            value = value.encode("utf-8")
        return base64.b64encode(value)


class BasicInfoSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        fields = ("url", "uuid", "name")
        extra_kwargs = {
            "url": {"lookup_field": "uuid"},
        }


class UnboundSerializerMethodField(ReadOnlyField):
    """
    A field that gets its value by calling a provided filter callback.
    """

    def __init__(self, filter_function, *args, **kwargs):
        self.filter_function = filter_function
        super().__init__(*args, **kwargs)

    def to_representation(self, value):
        request = self.context.get("request")
        return self.filter_function(value, request)


class GenericRelatedField(Field):
    """
    A custom field to use for the `tagged_object` generic relationship.
    """

    read_only = False
    _default_view_name = "%(model_name)s-detail"
    lookup_fields = ["uuid", "pk"]

    def __init__(self, related_models=(), **kwargs):
        super().__init__(**kwargs)
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
            "app_label": obj._meta.app_label,
        }
        try:
            format_kwargs["model_name"] = getattr(obj.__class__, "get_url_name")()
        except AttributeError:
            format_kwargs["model_name"] = obj._meta.object_name.lower()
        return self._default_view_name % format_kwargs

    def _get_request(self):
        try:
            return self.context["request"]
        except KeyError:
            raise AttributeError(
                "GenericRelatedField have to be initialized with `request` in context"
            )

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
            raise AttributeError("Related object does not have any of lookup_fields")
        if self.related_models and not isinstance(obj, tuple(self.related_models)):
            return None
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
            raise serializers.ValidationError(_("URL is invalid: %s.") % data)
        except (
            Resolver404,
            AttributeError,
            MultipleObjectsReturned,
            ObjectDoesNotExist,
        ):
            raise serializers.ValidationError(
                _("Can't restore object from url: %s") % data
            )

        if self.related_models and model not in self.related_models:
            context = (model, ", ".join(str(model) for model in self.related_models))
            message = _("%s is not valid. Valid models are: %s") % context
            raise serializers.ValidationError(message)

        return obj


class AugmentedSerializerMixin:
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
                class Meta:
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
                class Meta:
                    model = models.Project
                    fields = (
                        'url', 'uuid', 'name',
                        'customer', 'customer_uuid', 'customer_name',
                    )
                    lookup_field = 'uuid'

            # The fields of related object can be customized:

            class ProjectSerializer(AugmentedSerializerMixin,
                                    serializers.HyperlinkedModelSerializer):
                class Meta:
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
                class Meta:
                    model = models.Project
                    fields = ('url', 'uuid', 'name', 'customer')
                    protected_fields = ('customer',)

    4. This mixin overrides "get_extra_kwargs" method and puts "view_name" to extra_kwargs
    or uses URL name specified in a model of serialized object.
    """

    def get_fields(self):
        fields = super().get_fields()
        pre_serializer_fields.send(
            sender=self.__class__, fields=fields, serializer=self
        )

        try:
            protected_fields = self.Meta.protected_fields
        except AttributeError:
            pass
        else:
            try:
                method = self.context["view"].request.method
            except (KeyError, AttributeError):
                return fields

            if method in ("PUT", "PATCH"):
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
                "related_paths can be defined only for ModelSerializer."
            )

        if isinstance(related_paths, (list, tuple)):
            related_paths = {path: ("name", "uuid") for path in related_paths}

        return related_paths

    def build_unknown_field(self, field_name, model_class):
        related_paths = self._get_related_paths()

        related_field_source_map = {
            "{}_{}".format(path.split(".")[-1], attribute): f"{path}.{attribute}"
            for path, attributes in related_paths.items()
            for attribute in attributes
        }

        try:
            return (
                serializers.ReadOnlyField,
                {"source": related_field_source_map[field_name]},
            )
        except KeyError:
            return super().build_unknown_field(field_name, model_class)

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()

        if hasattr(self.Meta, "view_name"):
            view_name = self.Meta.view_name
        else:
            view_name = core_utils.get_detail_view_name(self.Meta.model)

        if "url" in extra_kwargs:
            extra_kwargs["url"]["view_name"] = view_name
        else:
            extra_kwargs["url"] = {"view_name": view_name}

        return extra_kwargs


class RestrictedSerializerMixin:
    """
    This mixin allows to specify list of fields to be rendered by serializer.
    It expects that request is available in serializer's context.
    """

    FIELDS_PARAM_NAME = "field"

    def get_fields(self):
        fields = super().get_fields()
        if "request" not in self.context:
            return fields
        query_params = self.context["request"].query_params
        keys = query_params.getlist(self.FIELDS_PARAM_NAME)
        keys = set(key for key in keys if key in fields.keys())
        optional_fields = set(self.get_optional_fields()) - keys
        fields = OrderedDict(
            (
                (key, value)
                for key, value in fields.items()
                if key not in optional_fields
            )
        )
        if not keys:
            return fields
        return OrderedDict(
            ((key, value) for key, value in fields.items() if key in keys)
        )

    def get_optional_fields(self):
        return []


class HyperlinkedRelatedModelSerializer(serializers.HyperlinkedModelSerializer):
    def __init__(self, **kwargs):
        self.queryset = kwargs.pop("queryset", None)
        assert self.queryset is not None or kwargs.get("read_only", None), (
            "Relational field must provide a `queryset` argument, "
            "or set read_only=`True`."
        )
        assert not (self.queryset is not None and kwargs.get("read_only", None)), (
            "Relational fields should not provide a `queryset` argument, "
            "when setting read_only=`True`."
        )
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if "url" not in data:
            raise serializers.ValidationError(
                _("URL has to be defined for related object.")
            )
        url_field = self.fields["url"]

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

        return url.to_internal_value(data["url"])


class UnicodeIntegerField(serializers.IntegerField):
    def to_internal_value(self, data):
        if isinstance(data, str):
            data = core_utils.normalize_unicode(data)
        return super().to_internal_value(data)


class DateRangeFilterSerializer(serializers.Serializer):
    start = core_fields.YearMonthField(required=False)
    end = core_fields.YearMonthField(required=False)

    def validate(self, data):
        if "start" in data and "end" in data and data["start"] > data["end"]:
            raise serializers.ValidationError(
                _("Start date must be earlier or equal to end date.")
            )

        if ("start" in data) ^ ("end" in data):
            raise serializers.ValidationError(_("Both parameters must be specified."))
        return data


class ReviewCommentSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False)


COLOR_HEX_RE = re.compile("^#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})$")
color_hex_validator = RegexValidator(
    COLOR_HEX_RE,
    _("Enter a valid hex color, eg. #000000"),
    "invalid",
)


class ConstanceSettingsSerializer(serializers.Serializer):
    def get_fields(self):
        fields = OrderedDict()

        for name, options in settings.CONFIG.items():
            default = options[0]
            if len(options) == 3:
                config_type = options[2]
                if config_type not in settings.ADDITIONAL_FIELDS and not isinstance(
                    default, config_type
                ):
                    raise ImproperlyConfigured(
                        _(
                            "Default value type must be "
                            "equal to declared config "
                            "parameter type. Please fix "
                            "the default value of "
                            "'%(name)s'."
                        )
                        % {"name": name}
                    )
            else:
                config_type = type(default)
            field_class = None
            if config_type == str:
                field_class = serializers.CharField
            if config_type == "image_field":
                field_class = serializers.ImageField
            if config_type == "email_field":
                field_class = serializers.EmailField
            if config_type == int:
                field_class = serializers.IntegerField
            if config_type == bool:
                field_class = serializers.BooleanField
            if not field_class:
                continue
            kwargs = dict(required=False)
            if config_type == str:
                kwargs["allow_blank"] = True
            if config_type == "image_field":
                kwargs["allow_null"] = True
            if name in ["BRAND_COLOR", "BRAND_LABEL_COLOR"]:
                kwargs["validators"] = [color_hex_validator]
            if name in [
                "HERO_LINK_URL",
                "DOCS_URL",
                "SUPPORT_PORTAL_URL",
                "ATLASSIAN_API_URL",
                "ZAMMAD_API_URL",
            ]:
                kwargs["validators"] = [URLValidator()]
            fields[name] = field_class(**kwargs)
        return fields

    def save(self):
        for name in self.fields.keys():
            current = getattr(config, name)
            if name not in self.validated_data:
                continue
            new = self.validated_data[name]
            if current != new:
                if hasattr(new, "name"):
                    new = default_storage.save(
                        join(django_settings.MEDIA_ROOT, new.name), new
                    )
                setattr(config, name, new)


class TranslatedModelSerializerMixin(serializers.ModelSerializer):
    def get_field_names(self, declared_fields, info):
        fields = list(super().get_field_names(declared_fields, info))
        trans_fields = get_translatable_fields_for_model(self.Meta.model)
        if not trans_fields:
            return fields

        all_fields = []
        for field_name in fields:
            all_fields.append(field_name)
            if field_name in trans_fields:
                for language_name in django_settings.LANGUAGE_CHOICES:
                    all_fields.append(f"{field_name}_{language_name}")
        return all_fields
