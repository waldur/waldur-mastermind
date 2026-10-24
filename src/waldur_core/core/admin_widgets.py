"""Admin widgets and the form field every admin edits JSON with.

Kept apart from waldur_core.core.admin, which registers model admins on import,
so that CoreConfig.ready() can install the JSON field without registering
anything early.
"""

import copy
import functools
import json
import math
import re

from django import forms
from django.conf import settings
from django.contrib.admin.options import BaseModelAdmin
from django.core.exceptions import ValidationError
from django.db import models
from django.forms import fields as forms_fields
from django.forms.utils import flatatt
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from waldur_core.core import fields as core_fields


class CopyButtonMixin:
    """Mixin to add copy-to-clipboard functionality to form fields in Django admin."""

    # Set by the forms.Widget this is mixed into.
    attrs: dict

    class Media:
        js = (settings.STATIC_URL + "landing/js/copy2clipboard.js",)

    def render(self, name, value, attrs=None, renderer=None):
        result = super().render(name, value, attrs, renderer)  # type: ignore[misc]
        target = (attrs or {}).get("id") or self.attrs.get("id")
        if not target:
            # Nothing to point the button at, so no button.
            return result
        button_attrs = {
            "class": "button copy-button",
            "data-target-id": target,
        }
        result += f"<a {flatatt(button_attrs)}>Copy</a>"
        return mark_safe(result)  # noqa: S308


# A UTF-16 surrogate in a Python string is always unpaired: it cannot be sent
# as UTF-8, and PostgreSQL refuses it, as it refuses NUL.
_SURROGATE = re.compile("[\ud800-\udfff]")
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")

JSON_MODEL_FIELDS = (models.JSONField, core_fields.JSONField)


def _json_for_display(value, encoder=None):
    text = json.dumps(value, indent=2, ensure_ascii=False, cls=encoder)
    # Escaped, a lone surrogate survives the page; the rest stays readable.
    return _SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def _reject_constant(name):
    raise ValueError(f"{name} is not JSON")


def _finite_float(text):
    number = float(text)
    if not math.isfinite(number):
        # 1e999 overflows to infinity, which neither jsonb nor the API takes.
        raise ValueError(f"{text} is out of range")
    return number


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _storable(value):
    if isinstance(value, str):
        return not _UNSTORABLE.search(value)
    if isinstance(value, dict):
        return all(_storable(key) and _storable(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_storable(item) for item in value)
    return True


class JsonWidget(CopyButtonMixin, forms.Textarea):
    """JSON as text in a monospace textarea sized to its content.

    The value arrives already laid out by AdminJsonField; anything else that is
    not text is pretty-printed here.
    """

    MIN_ROWS = 4
    MAX_ROWS = 40
    # About what a vLargeTextField holds in a monospace font.
    COLUMNS = 80

    def __init__(self, attrs=None):
        attrs = dict(attrs or {})
        self.fixed_rows = "rows" in attrs
        attrs.setdefault("cols", self.COLUMNS)
        attrs["class"] = " ".join(filter(None, ["vLargeTextField", attrs.get("class")]))
        attrs["style"] = "; ".join(
            filter(None, ["font-family: monospace", attrs.get("style")])
        )
        super().__init__(attrs)

    @property
    def media(self):
        # The jQuery build the admin itself loads, so the page has only one.
        extra = "" if settings.DEBUG else ".min"
        return forms.Media(
            js=(
                f"admin/js/vendor/jquery/jquery{extra}.js",
                "admin/js/jquery.init.js",
                *CopyButtonMixin.Media.js,
            )
        )

    def format_value(self, value):
        if value is None or isinstance(value, str):
            return value
        return _json_for_display(value)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        widget_attrs = context["widget"]["attrs"]
        if not self.fixed_rows:
            columns = int(widget_attrs.get("cols") or self.COLUMNS)
            text = context["widget"]["value"] or ""
            # Long lines wrap, so each takes as many rows as it needs.
            lines = sum(
                max(1, math.ceil(len(line) / columns)) for line in text.split("\n")
            )
            widget_attrs["rows"] = min(max(lines + 1, self.MIN_ROWS), self.MAX_ROWS)
        return context


class AdminJsonField(forms.JSONField):
    """JSON edited in the admin as pretty-printed text.

    Parsing is strict: NaN and infinities, duplicate keys, NUL and unpaired
    surrogates are refused, since PostgreSQL or every later API read would fail
    on them.

    What a field takes comes from its model field (see
    admin_json_formfield_kwargs): cleared text stores the model's default,
    or null where the column takes it; null is refused where it does not; and
    a field whose default is an object holds an object.

    A value stored before these rules, such as a list in an object field, is
    kept while nobody edits it, so it does not block saving the rest of the
    form. A stored null where the column has a default is shown, and saved,
    as that default.
    """

    widget = JsonWidget
    default_error_messages = {
        "not_object": _("Enter a JSON object, such as {}."),
        "null": _("This field cannot be null."),
        "unstorable": _(
            "Text in the JSON may not contain NUL characters or unpaired surrogates."
        ),
    }

    def __init__(self, *, holds_object=False, allow_null=True, empty=None, **kwargs):
        # A text-backed model field passes a max_length, a JSON field has none.
        kwargs.pop("max_length", None)
        super().__init__(**kwargs)
        self.holds_object = holds_object
        self.allow_null = allow_null
        # Called for a fresh value each time; None stores null.
        self.empty = empty

    def _empty_value(self):
        return self.empty() if self.empty else None

    def _shown(self, value):
        """What a stored value is shown and compared as."""
        if value is None and self.empty and not self.allow_null:
            return self._empty_value()
        return value

    def _dump(self, value):
        return json.dumps(value, sort_keys=True, cls=self.encoder)

    def to_python(self, value):
        if self.disabled:
            return value
        if value in self.empty_values:
            return self._empty_value()
        if isinstance(value, str) and not isinstance(value, forms_fields.JSONString):
            try:
                value = json.loads(
                    value,
                    cls=self.decoder,
                    parse_constant=_reject_constant,
                    parse_float=_finite_float,
                    object_pairs_hook=_unique_object,
                )
            except (ValueError, RecursionError):
                raise ValidationError(
                    self.error_messages["invalid"],
                    code="invalid",
                    params={"value": value},
                )
            if isinstance(value, str):
                value = forms_fields.JSONString(value)
        if not _storable(value):
            raise ValidationError(self.error_messages["unstorable"], code="unstorable")
        return value

    def validate(self, value):
        if self.holds_object and not isinstance(value, dict):
            raise ValidationError(self.error_messages["not_object"], code="not_object")
        super().validate(value)
        if value is None and not self.allow_null:
            raise ValidationError(self.error_messages["null"], code="null")

    def _clean_bound_field(self, bf):
        if self.disabled:
            return super()._clean_bound_field(bf)  # type: ignore[attr-defined]
        value = self.to_python(bf.data)
        stored = self._shown(bf.initial)
        if self._dump(value) == self._dump(stored) and value is not None:
            # Unchanged: what is stored stays, even where today's rules would
            # refuse it, so an old value does not block the rest of the form.
            return value
        self.validate(value)
        self.run_validators(value)
        return value

    def bound_data(self, data, initial):
        return super().bound_data(data, self._shown(initial))

    def prepare_value(self, value):
        if isinstance(value, forms_fields.InvalidJSONInput):
            return value
        return _json_for_display(self._shown(value), self.encoder)

    def has_changed(self, initial, data):
        if self.disabled:
            return False
        try:
            value = self.to_python(data)
        except ValidationError:
            return True
        return self._dump(self._shown(initial)) != self._dump(value)


def _empty_factory(db_field):
    """What cleared text stores: a fresh copy of the model default, if any."""
    if not db_field.has_default():
        return None
    if callable(db_field.default):
        return db_field.default
    return functools.partial(copy.deepcopy, db_field.default)


def admin_json_formfield_kwargs(db_field):
    """The AdminJsonField a JSON model field is edited with, from that field."""
    default = db_field.default
    kwargs = {
        "form_class": AdminJsonField,
        "widget": JsonWidget,
        "holds_object": default is dict or isinstance(default, dict),
        "allow_null": db_field.null,
        "empty": _empty_factory(db_field),
    }
    if isinstance(db_field, core_fields.JSONField):
        # The legacy text-backed field keeps its own (de)serialisation.
        kwargs["encoder"] = db_field.dump_kwargs.get("cls")
        kwargs["decoder"] = db_field.load_kwargs.get("cls")
    return kwargs


def install_admin_json_fields():
    """Edit every JSON model field in every admin, inlines included, with
    AdminJsonField.

    The admin's own FORMFIELD_FOR_DBFIELD_DEFAULTS can only hand every field of
    a class the same arguments, and what a field takes depends on its model
    field. Wrapping formfield_for_dbfield, called per request after setup,
    reaches every ModelAdmin and inline whatever the app order. Arguments a
    form or admin passes itself still win.
    """
    original = BaseModelAdmin.formfield_for_dbfield
    if getattr(original, "installs_admin_json_fields", False):
        return

    @functools.wraps(original)
    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if isinstance(db_field, JSON_MODEL_FIELDS) and not db_field.choices:
            kwargs = {**admin_json_formfield_kwargs(db_field), **kwargs}
        return original(self, db_field, request, **kwargs)

    formfield_for_dbfield.installs_admin_json_fields = True  # type: ignore[attr-defined]
    BaseModelAdmin.formfield_for_dbfield = formfield_for_dbfield
