import json

from django import forms
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import default_storage
from django.utils.module_loading import import_string
from django.utils.text import normalize_newlines

from waldur_core.configuration import registry
from waldur_core.configuration.models import Setting, Source, locked_message
from waldur_core.core.validators import collect_access_email_pattern_errors
from waldur_core.media.validators import validate_notification_emails

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


# Lists the settings the page showed read-only. A disabled input is not
# posted, and a lock released after the page was loaded must not turn that
# missing input into an empty value.
READ_ONLY_FIELD = "read_only_settings"


def locked_field(key, manager):
    """A read-only text field showing a setting the deployment manages.

    Not the setting's own field disabled: that field would clean the stored
    value as if it were posted, which dict, list and image fields cannot do.
    """
    if manager is None:
        reason = "Read-only when this page was loaded; reload it to edit."
    else:
        reason = locked_message(key, manager)
    return forms.CharField(
        label=key,
        help_text=f"{reason} {registry.help_text(key)}",
        required=False,
        disabled=True,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


def _differs(key, new, old):
    if registry.field_type(key) == "multiple_choice_field":
        return set(new or ()) != set(old or ())
    return new != old


class SettingsForm(forms.Form):
    """Every setting on one form, as the Django admin edits them."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        locks = Setting.objects.locks(*registry.definitions())
        shown_read_only = (
            self.data.get(READ_ONLY_FIELD, "").split(",") if self.is_bound else ()
        )
        self.locked = {
            **{key: None for key in shown_read_only if registry.has(key)},
            **locks,
        }
        # Locked after the page was loaded: their inputs were editable, and
        # what was typed into them is not saved.
        self.locked_since_load = (
            [key for key in locks if key not in shown_read_only]
            if self.is_bound
            else []
        )
        self.initial = dict(self.initial)
        self.fields[READ_ONLY_FIELD] = forms.CharField(
            required=False,
            widget=forms.HiddenInput,
            initial=",".join(sorted(self.locked)),
        )
        for key in registry.definitions():
            if key in self.locked:
                self.fields[key] = locked_field(key, self.locked[key])
                value = self.initial.get(key)
                if registry.is_secret(key):
                    value = "********" if value else ""
                elif isinstance(value, dict | list):
                    value = json.dumps(value)
                self.initial[key] = value
            else:
                self.fields[key] = form_field(key)

    def clean_OIDC_ALLOWED_USER_EMAIL_PATTERNS(self):
        # Mirrors ConstanceSettingsSerializer: an unusable pattern never matches,
        # so accepting a typo here would lock users out instead of letting them in.
        value = self.cleaned_data["OIDC_ALLOWED_USER_EMAIL_PATTERNS"]
        if "OIDC_ALLOWED_USER_EMAIL_PATTERNS" in self.locked:
            return value
        errors = collect_access_email_pattern_errors(value)
        if errors:
            raise forms.ValidationError(errors)
        return value

    def clean_VERSION_UPDATE_NOTIFICATION_EMAILS(self):
        # Mirrors ConstanceSettingsSerializer: a malformed address would only
        # surface as a bounce, after the release it was meant to announce.
        # Same rule as Customer.notification_emails.
        return validate_notification_emails(
            self.cleaned_data["VERSION_UPDATE_NOTIFICATION_EMAILS"]
        )

    def _posted(self, key):
        """What was typed into the input of ``key``, cleaned by its own
        field; None if it does not clean."""
        field = form_field(key)
        try:
            value = field.clean(
                field.widget.value_from_datadict(self.data, self.files, key)
            )
        except forms.ValidationError:
            return None
        return normalize_newlines(value) if isinstance(value, str) else value

    def save(self, user=None):
        """Store the settings whose value changed. Returns the settings that
        were changed on the page but not stored, because the deployment
        locked them after the page was loaded."""
        current = Setting.objects.values_with_defaults()
        not_saved = [
            key
            for key in self.locked_since_load
            if registry.field_type(key) != "image_field"
            and _differs(key, self._posted(key), current[key])
        ]
        for key in self.files:
            upload = self.cleaned_data.get(key)
            if upload:
                self.cleaned_data[key] = default_storage.save(upload.name, upload)
        for key in registry.definitions():
            if key not in self.cleaned_data or key in self.locked:
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
        return not_saved
