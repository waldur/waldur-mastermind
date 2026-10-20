from django import forms
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import default_storage
from django.utils.module_loading import import_string
from django.utils.text import normalize_newlines

from waldur_core.configuration import registry
from waldur_core.configuration.models import Setting, Source
from waldur_core.core.validators import collect_access_email_pattern_errors

NUMERIC_WIDGET = {"widget": forms.TextInput(attrs={"size": 10})}

PYTHON_TYPE_FIELDS = {
    bool: (forms.BooleanField, {"required": False}),
    int: (forms.IntegerField, NUMERIC_WIDGET),
    float: (forms.FloatField, NUMERIC_WIDGET),
    str: (
        forms.CharField,
        {"widget": forms.Textarea(attrs={"rows": 3}), "required": False},
    ),
}


def _additional_field(field_type):
    path, *rest = registry.additional_fields()[field_type]
    kwargs = dict(rest[0]) if rest else {}
    if "widget" in kwargs:
        widget_class = import_string(kwargs["widget"])
        kwargs["widget"] = widget_class(**(kwargs.pop("widget_kwargs", None) or {}))
    return import_string(path), kwargs


def form_field(key):
    """The form field that edits setting ``key``."""
    field_type = registry.field_type(key)
    if field_type is None:
        field_type = type(registry.default(key))
    if isinstance(field_type, str) and field_type in registry.additional_fields():
        field_class, kwargs = _additional_field(field_type)
    elif field_type in PYTHON_TYPE_FIELDS:
        field_class, kwargs = PYTHON_TYPE_FIELDS[field_type]
        kwargs = dict(kwargs)
    else:
        raise ImproperlyConfigured(
            f"Setting {key} has a type no form field is known for: {field_type}"
        )

    choices = registry.choices().get(key)
    if field_type == "multiple_choice_field":
        return forms.MultipleChoiceField(
            label=key,
            help_text=registry.help_text(key),
            choices=list(choices or []),
            required=False,
        )
    if choices is not None and issubclass(field_class, forms.ChoiceField):
        kwargs["choices"] = list(choices)
    return field_class(label=key, help_text=registry.help_text(key), **kwargs)


class SettingsForm(forms.Form):
    """Every setting on one form, as the Django admin edits them."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key in registry.definitions():
            self.fields[key] = form_field(key)

    def clean_OIDC_ALLOWED_USER_EMAIL_PATTERNS(self):
        # Mirrors ConstanceSettingsSerializer: an unusable pattern never matches,
        # so accepting a typo here would lock users out instead of letting them in.
        value = self.cleaned_data["OIDC_ALLOWED_USER_EMAIL_PATTERNS"]
        errors = collect_access_email_pattern_errors(value)
        if errors:
            raise forms.ValidationError(errors)
        return value

    def save(self, user=None):
        """Store the settings whose value changed."""
        for key in self.files:
            upload = self.cleaned_data.get(key)
            if upload:
                self.cleaned_data[key] = default_storage.save(upload.name, upload)
        current = Setting.objects.values_with_defaults()
        for key in registry.definitions():
            if key not in self.cleaned_data:
                continue
            new = self.cleaned_data[key]
            if isinstance(new, str):
                new = normalize_newlines(new)
            # An image field left untouched cleans to None or False; neither
            # is a new value.
            if registry.field_type(key) == "image_field" and not new:
                continue
            if current[key] != new:
                Setting.objects.set(key, new, source=Source.ADMIN, user=user)
