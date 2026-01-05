import base64
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping

from constance import LazyConfig, settings
from django import forms
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
from rest_framework import serializers as rf_serializers
from rest_framework.fields import Field, ReadOnlyField
from rest_framework.serializers import ListSerializer

from waldur_core.core import utils as core_utils
from waldur_core.core.clean_html import clean_html
from waldur_core.core.models import generate_slug
from waldur_core.core.signals import pre_serializer_fields
from waldur_mastermind.common.serializers import StringListSerializer

from . import fields as core_fields

logger = logging.getLogger(__name__)
config = LazyConfig()


class DictField(forms.CharField):
    def __init__(self, *args, **kwargs):
        kwargs["widget"] = forms.Textarea
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if not value:
            return {}
        try:
            return json.loads(value)
        except ValueError as e:
            raise forms.ValidationError(f"Invalid JSON format: {str(e)}")

    def prepare_value(self, value):
        if value is None:
            return ""
        if isinstance(value, dict):
            try:
                return json.dumps(value, indent=2)
            except (TypeError, ValueError) as e:
                raise forms.ValidationError(f"Could not serialize dictionary: {str(e)}")
        return value


class ListField(forms.CharField):
    def __init__(self, *args, **kwargs):
        kwargs["widget"] = forms.TextInput
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if not value:
            return []
        try:
            return [item.strip() for item in value.split(",")]
        except ValueError as e:
            raise forms.ValidationError(f"Invalid string format: {str(e)}")

    def prepare_value(self, value):
        if value is None:
            return ""
        if isinstance(value, list):
            return ", ".join(value)
        return str(value)


class JsonListField(forms.CharField):
    """
    A form field for handling JSON lists that can contain dictionaries or any JSON-serializable items.
    Used by constance admin for settings that need to store lists of objects.
    """

    def __init__(self, *args, **kwargs):
        kwargs["widget"] = forms.Textarea
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        if not value:
            return []
        try:
            if isinstance(value, str):
                return json.loads(value)
            return value
        except ValueError as e:
            raise forms.ValidationError(f"Invalid JSON format: {str(e)}")

    def prepare_value(self, value):
        if value is None:
            return "[]"
        if isinstance(value, list):
            try:
                return json.dumps(value, indent=2)
            except (TypeError, ValueError) as e:
                raise forms.ValidationError(f"Could not serialize list: {str(e)}")
        return value


class DictSerializerField(serializers.CharField):
    def to_internal_value(self, data):
        """Convert JSON string to Python dictionary."""
        if not data:
            return {}
        try:
            return json.loads(data) if isinstance(data, str) else data
        except ValueError as e:
            raise serializers.ValidationError(f"Invalid JSON format: {str(e)}")

    def to_representation(self, value):
        """Convert Python dictionary to JSON string for representation."""
        if isinstance(value, dict):
            try:
                return json.dumps(value, indent=2)
            except (TypeError, ValueError) as e:
                raise serializers.ValidationError(
                    f"Could not serialize dictionary: {str(e)}"
                )
        return value


class JsonListSerializerField(serializers.ListField):
    """
    A field for handling JSON lists that can contain dictionaries or any JSON-serializable items.
    Unlike StringListSerializer which only accepts strings, this accepts any JSON array.
    """

    def to_internal_value(self, data):
        """Convert JSON string to Python list or pass through if already a list."""
        if not data:
            return []
        try:
            if isinstance(data, str):
                return json.loads(data)
            return data
        except ValueError as e:
            raise serializers.ValidationError(f"Invalid JSON format: {str(e)}")

    def to_representation(self, value):
        """Return the list as-is for JSON serialization."""
        if value is None:
            return []
        return value


class MultilingualImageSerializerField(serializers.DictField):
    """
    A field for handling language-specific image uploads.

    Accepts a dictionary where keys are language codes and values are image files.
    Example: {"de": <UploadedFile>, "et": <UploadedFile>}

    Returns a dictionary of language codes to stored file paths.
    """

    child = serializers.ImageField(allow_null=True, required=False)

    def to_internal_value(self, data):
        if not data:
            return {}

        # Handle JSON string input (for compatibility with existing data)
        if isinstance(data, str):
            try:
                return json.loads(data)
            except ValueError as e:
                raise serializers.ValidationError(f"Invalid JSON format: {str(e)}")

        if not isinstance(data, dict):
            raise serializers.ValidationError(
                "Expected a dictionary of language codes to images."
            )

        result = {}
        for lang_code, image in data.items():
            if not isinstance(lang_code, str) or len(lang_code) > 10:
                raise serializers.ValidationError(
                    f"Invalid language code '{lang_code}'. Must be a string (max 10 chars)."
                )
            # Allow None/empty to remove a language entry
            if image is None or image == "":
                continue
            # If it's already a string path, keep it (for partial updates)
            if isinstance(image, str):
                result[lang_code] = image
            else:
                # Validate it's a proper image file
                validated_image = self.child.run_validation(image)
                result[lang_code] = validated_image

        return result

    def to_representation(self, value):
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        return {}


class ObtainAuthTokenSerializer(serializers.Serializer):
    """
    API token serializer loosely based on DRF's default AuthTokenSerializer.
    but with the logic of authorization is extracted to view.
    """

    # Fields are both required, non-blank and don't allow nulls by default
    username = serializers.CharField(
        max_length=128, help_text="Username for authentication"
    )
    password = serializers.CharField(
        max_length=128, help_text="Password for authentication"
    )


class CoreAuthTokenSerializer(serializers.Serializer):
    token = serializers.CharField(
        read_only=True, help_text="Authentication token for API access"
    )


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
                customer_uuid = serializers.UUIDField(read_only=True, source='customer.uuid')
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
                    if field in fields:
                        fields[field].read_only = True

        return fields

    def _get_related_paths(self) -> Mapping[str, Iterable]:
        try:
            related_paths = self.Meta.related_paths
        except AttributeError:
            return {}

        if not isinstance(self, serializers.ModelSerializer):
            raise ImproperlyConfigured(
                "related_paths can be defined only for ModelSerializer."
            )

        if isinstance(related_paths, list | tuple):
            related_paths = {path: ("name", "uuid") for path in related_paths}

        return related_paths

    def build_unknown_field(self, field_name, model_class):
        related_paths = self._get_related_paths()

        related_field_source_map = {
            "{}_{}".format(path.split(".")[-1], attribute): f"{path}.{attribute}"
            for path, attributes in related_paths.items()
            for attribute in attributes
        }

        related_field_nullable_map = {
            "{}_{}".format(path.split(".")[-1], attribute): getattr(
                model_class._meta.get_field(path), "null", False
            )
            for path, attributes in related_paths.items()
            for attribute in attributes
        }

        if field_name not in related_field_source_map:
            return super().build_unknown_field(field_name, model_class)

        return (
            serializers.ReadOnlyField,
            {
                "source": related_field_source_map[field_name],
                "allow_null": related_field_nullable_map[field_name],
            },
        )

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

    It is disabled for nested serializers (where parent is another serializer)
    but remains active for list views (where parent is a ListSerializer).
    """

    FIELDS_PARAM_NAME = "field"

    def get_fields(self):
        if self.parent is not None and not isinstance(self.parent, ListSerializer):
            return super().get_fields()

        fields = super().get_fields()
        if "request" not in self.context:
            return fields

        # Check if this is schema generation context (drf-spectacular)
        # When generating schema, we want to include all fields
        request = self.context["request"]
        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        query_params = request.query_params

        keys = query_params.getlist(self.FIELDS_PARAM_NAME)
        keys = set(key for key in keys if key in fields.keys())
        fields = OrderedDict(((key, value) for key, value in fields.items()))
        if not keys:
            return fields
        return OrderedDict(
            ((key, value) for key, value in fields.items() if key in keys)
        )


class UnicodeIntegerField(serializers.IntegerField):
    def to_internal_value(self, data):
        if isinstance(data, str):
            data = core_utils.normalize_unicode(data)
        return super().to_internal_value(data)


class DateRangeFilterSerializer(serializers.Serializer):
    start = core_fields.YearMonthField(
        required=False, help_text="Start date in YYYY-MM format"
    )
    end = core_fields.YearMonthField(
        required=False, help_text="End date in YYYY-MM format"
    )

    def validate(self, data):
        if "start" in data and "end" in data and data["start"] > data["end"]:
            raise serializers.ValidationError(
                _("Start date must be earlier or equal to end date.")
            )

        if ("start" in data) ^ ("end" in data):
            raise serializers.ValidationError(_("Both parameters must be specified."))
        return data


class ReviewCommentSerializer(serializers.Serializer):
    comment = serializers.CharField(
        required=False, help_text="Optional comment for review"
    )


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
            if config_type is str:
                field_class = serializers.CharField
            if config_type == "image_field":
                field_class = serializers.ImageField
            if config_type == "email_field":
                field_class = serializers.EmailField
            if config_type is int:
                field_class = serializers.IntegerField
            if config_type is bool:
                field_class = serializers.BooleanField
            if config_type == "dict_field":
                field_class = DictSerializerField
            if config_type == "multilingual_image_field":
                field_class = MultilingualImageSerializerField
            if config_type == "list_field":
                field_class = StringListSerializer
            if config_type == "json_list_field":
                field_class = JsonListSerializerField
            if config_type == "country_list_field":
                field_class = StringListSerializer
            if config_type in (
                "color_field",
                "html_field",
                "text_field",
                "url_field",
                "secret_field",
            ):
                field_class = serializers.CharField
            if not field_class:
                continue
            kwargs = dict(required=False)
            if config_type is str:
                kwargs["allow_blank"] = True
            if config_type == "image_field":
                kwargs["allow_null"] = True
            if config_type == "secret_field":
                kwargs["allow_blank"] = True
            if config_type == "color_field":
                kwargs["validators"] = [color_hex_validator]
                kwargs["allow_blank"] = True
            if config_type == "url_field":
                kwargs["validators"] = [URLValidator()]
                kwargs["allow_blank"] = True
            fields[name] = field_class(**kwargs)
        return fields

    def save(self):
        for name in self.validated_data.keys():
            # Skip fields that are not present in initial data which are sent via request
            if name not in self.initial_data:
                continue
            current = getattr(config, name)
            new = self.validated_data[name]
            if current != new:
                # Handle single image file
                if hasattr(new, "name"):
                    new = default_storage.save(new.name, new)
                # Handle dict of image files (multilingual_image_field)
                elif isinstance(new, dict):
                    processed = {}
                    for key, value in new.items():
                        if hasattr(value, "name"):
                            # It's an uploaded file, save it
                            processed[key] = default_storage.save(value.name, value)
                        else:
                            # It's already a path string
                            processed[key] = value
                    new = processed
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


class SlugSerializerMixin(serializers.Serializer):
    """
    Ensures that slug is editable only by staff
    """

    slug = serializers.SlugField(
        required=False,
        allow_blank=True,
        max_length=50,
        help_text="URL-friendly identifier. Only editable by staff users.",
    )

    def get_fields(self):
        fields = super().get_fields()

        try:
            request = self.context["request"]
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if getattr(self.context.get("view"), "swagger_fake_view", False):
            return fields

        if not user.is_staff:
            if "slug" in fields:
                fields["slug"].read_only = True

        return fields

    def validate_slug(self, value):
        """
        Validates that the slug is unique for the model.

        Staff can edit slug values, but they must be unique.
        """
        if not value:
            return value

        # Get the model class
        model_class = self.Meta.model

        # Check if this is an update or create operation
        instance = getattr(self, "instance", None)

        # Build the query to check for existing slugs
        queryset = model_class.objects.filter(slug=value)

        # If updating, exclude the current instance
        if instance:
            queryset = queryset.exclude(pk=instance.pk)

        # Check if slug already exists
        if queryset.exists():
            raise serializers.ValidationError(
                _("An object with this slug already exists.")
            )

        return value

    def generate_slug(self, validated_data):
        klass = self.Meta.model
        slug_source = validated_data[klass.get_slug_source_field()]
        return generate_slug(slug_source, klass)

    def create(self, validated_data):
        if "slug" not in validated_data:
            validated_data["slug"] = self.generate_slug(validated_data)
        return super().create(validated_data)


class EmptySerializer(rf_serializers.Serializer):
    pass


class TableSizeSerializer(serializers.Serializer):
    table_name = serializers.CharField(
        read_only=True, help_text="Name of the database table"
    )
    total_size = serializers.IntegerField(
        read_only=True, help_text="Total size of the table in bytes"
    )
    data_size = serializers.IntegerField(
        read_only=True, help_text="Size of the actual data in bytes"
    )
    external_size = serializers.IntegerField(
        read_only=True, help_text="Size of external data (e.g., TOAST) in bytes"
    )


class QuerySerializer(serializers.Serializer):
    query = serializers.CharField(help_text="Search query string")


class VersionSerializer(serializers.Serializer):
    version = serializers.CharField(
        help_text="Current installed version of the application"
    )
    latest_version = serializers.CharField(
        help_text="Latest available version from GitHub, if available.", required=False
    )


class LogoutSerializer(serializers.Serializer):
    logout_url = serializers.URLField(
        read_only=True, help_text="URL to redirect to after logout"
    )


class CeleryTaskSerializer(serializers.Serializer):
    """Serializer for a single Celery task."""

    id = serializers.CharField(read_only=True, help_text="Unique task identifier")
    name = serializers.CharField(read_only=True, help_text="Name of the task")
    args = serializers.ListField(
        child=serializers.JSONField(),
        read_only=True,
        help_text="Positional arguments passed to the task",
        required=False,
    )
    kwargs = serializers.DictField(
        read_only=True,
        help_text="Keyword arguments passed to the task",
        required=False,
    )
    type = serializers.CharField(read_only=True, help_text="Task type", required=False)
    hostname = serializers.CharField(
        read_only=True, help_text="Worker hostname executing the task", required=False
    )
    time_start = serializers.FloatField(
        read_only=True, help_text="Unix timestamp when task started", required=False
    )
    acknowledged = serializers.BooleanField(
        read_only=True, help_text="Whether task has been acknowledged", required=False
    )
    delivery_info = serializers.DictField(
        read_only=True, help_text="Message delivery information", required=False
    )
    worker_pid = serializers.IntegerField(
        read_only=True, help_text="Worker process ID", required=False
    )


class CeleryScheduledTaskSerializer(serializers.Serializer):
    """Serializer for a scheduled Celery task with ETA information."""

    eta = serializers.CharField(
        read_only=True, help_text="Estimated time of arrival for the task"
    )
    priority = serializers.IntegerField(
        read_only=True, help_text="Task priority level", required=False
    )
    request = CeleryTaskSerializer(read_only=True, help_text="Task request details")


class CeleryWorkerPoolSerializer(serializers.Serializer):
    """Serializer for Celery worker pool statistics."""

    max_concurrency = serializers.IntegerField(
        read_only=True, help_text="Maximum number of concurrent processes"
    )
    processes = serializers.ListField(
        child=serializers.IntegerField(),
        read_only=True,
        help_text="List of worker process IDs",
    )
    max_tasks_per_child = serializers.IntegerField(
        read_only=True, help_text="Maximum tasks per child process", required=False
    )
    put_guarded_by_semaphore = serializers.BooleanField(read_only=True, required=False)
    timeouts = serializers.ListField(
        child=serializers.IntegerField(),
        read_only=True,
        help_text="Timeout values",
        required=False,
    )
    writes = serializers.DictField(
        read_only=True, help_text="Write statistics", required=False
    )


class CeleryBrokerSerializer(serializers.Serializer):
    """Serializer for Celery broker connection information."""

    hostname = serializers.CharField(
        read_only=True, help_text="Broker hostname", required=False
    )
    userid = serializers.CharField(
        read_only=True, help_text="Broker user ID", required=False
    )
    virtual_host = serializers.CharField(
        read_only=True, help_text="Virtual host", required=False
    )
    port = serializers.IntegerField(
        read_only=True, help_text="Broker port", required=False
    )
    insist = serializers.BooleanField(read_only=True, required=False)
    ssl = serializers.BooleanField(read_only=True, required=False)
    transport = serializers.CharField(
        read_only=True, help_text="Transport protocol", required=False
    )
    connect_timeout = serializers.IntegerField(
        read_only=True, help_text="Connection timeout in seconds", required=False
    )
    transport_options = serializers.DictField(
        read_only=True, help_text="Additional transport options", required=False
    )
    login_method = serializers.CharField(
        read_only=True, help_text="Authentication method", required=False
    )
    uri_prefix = serializers.CharField(read_only=True, required=False)
    heartbeat = serializers.FloatField(
        read_only=True, help_text="Heartbeat interval", required=False
    )
    failover_strategy = serializers.CharField(read_only=True, required=False)
    alternates = serializers.ListField(
        child=serializers.CharField(), read_only=True, required=False
    )


class CeleryWorkerStatsSerializer(serializers.Serializer):
    """Serializer for individual Celery worker statistics."""

    broker = CeleryBrokerSerializer(
        read_only=True, help_text="Broker connection information", required=False
    )
    clock = serializers.CharField(
        read_only=True, help_text="Logical clock value", required=False
    )
    uptime = serializers.FloatField(
        read_only=True, help_text="Worker uptime in seconds", required=False
    )
    pid = serializers.IntegerField(
        read_only=True, help_text="Worker process ID", required=False
    )
    pool = CeleryWorkerPoolSerializer(
        read_only=True, help_text="Worker pool statistics", required=False
    )
    prefetch_count = serializers.IntegerField(
        read_only=True, help_text="Number of tasks prefetched", required=False
    )
    rusage = serializers.DictField(
        read_only=True, help_text="Resource usage statistics", required=False
    )
    total = serializers.DictField(
        read_only=True, help_text="Total task counts by type", required=False
    )


class CeleryStatsResponseSerializer(serializers.Serializer):
    """
    Response serializer for Celery worker statistics.

    Each field is a dictionary where keys are worker names (e.g., 'celery@hostname')
    and values contain the respective task or statistics information.
    """

    active = serializers.DictField(
        child=serializers.ListField(child=CeleryTaskSerializer()),
        read_only=True,
        allow_null=True,
        help_text="Currently executing tasks per worker. Keys are worker names, values are lists of active tasks.",
    )
    scheduled = serializers.DictField(
        child=serializers.ListField(child=CeleryScheduledTaskSerializer()),
        read_only=True,
        allow_null=True,
        help_text="Tasks scheduled for future execution per worker. Keys are worker names, values are lists of scheduled tasks with ETA.",
    )
    reserved = serializers.DictField(
        child=serializers.ListField(child=CeleryTaskSerializer()),
        read_only=True,
        allow_null=True,
        help_text="Tasks that have been received but not yet started per worker. Keys are worker names, values are lists of reserved tasks.",
    )
    revoked = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        read_only=True,
        allow_null=True,
        help_text="IDs of revoked (cancelled) tasks per worker. Keys are worker names, values are lists of task IDs.",
    )
    query_task = serializers.DictField(
        read_only=True,
        allow_null=True,
        help_text="Query results for specific tasks. May be null if no query was performed.",
    )
    stats = serializers.DictField(
        child=CeleryWorkerStatsSerializer(),
        read_only=True,
        allow_null=True,
        help_text="Detailed statistics per worker including uptime, pool info, and resource usage. Keys are worker names.",
    )


class HTMLCleanField(serializers.CharField):
    """
    A CharField that automatically cleans HTML content using the clean_html utility.

    This field ensures consistent HTML sanitization across the application by
    automatically cleaning any HTML content that is provided to it.

    Usage:
        class MySerializer(serializers.ModelSerializer):
            description = HTMLCleanField()
            content = HTMLCleanField(required=False, allow_blank=True)

            class Meta:
                model = MyModel
                fields = ('description', 'content')
    """

    def to_internal_value(self, data):
        # First, let the parent CharField handle basic validation
        value = super().to_internal_value(data)

        # Then clean the HTML content if it's not empty
        if value:
            return clean_html(value.strip())

        return value
