import enum
import json
import uuid
from decimal import Decimal
from typing import Any

import django
import orjson
from django.utils.functional import Promise
from drf_orjson_renderer.renderers import ORJSONRenderer as BaseORJSONRenderer
from rest_framework import renderers
from rest_framework.settings import api_settings

from waldur_core import __version__
from waldur_core.core.fields import StringUUID

# ChoicesType: drf_orjson uses django.VERSION <= (6, 0) which fails for 6.0.x
if django.VERSION < (5, 0):
    from django.db.models.enums import ChoicesMeta as ChoicesType
else:
    from django.db.models.enums import ChoicesType


def _orjson_default(obj: Any) -> Any:
    """
    Handle types that orjson cannot serialize natively.
    Reimplements drf_orjson_renderer logic to avoid ChoicesType NameError.
    """
    if isinstance(obj, StringUUID):
        return str(obj)
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, list):
        return list(obj)
    if isinstance(obj, Decimal):
        return str(obj) if api_settings.COERCE_DECIMAL_TO_STRING else float(obj)
    # Promise=lazy i18n, ChoicesType=enum class, enum.Enum=enum members
    if isinstance(obj, str | uuid.UUID | Promise | ChoicesType | enum.Enum):
        return str(obj)
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if hasattr(obj, "__iter__"):
        return list(obj)


class WaldurORJSONRenderer(BaseORJSONRenderer):
    """
    ORJSON renderer with support for Waldur's StringUUID and Django 6.0.x.

    - orjson raises for uuid.UUID subclasses like StringUUID
    - drf_orjson_renderer has ChoicesType NameError on Django 6.0.2
    """

    def render(
        self,
        data: Any,
        media_type: str | None = None,
        renderer_context: dict | None = None,
    ) -> bytes:
        if data is None:
            return b""

        # OPT_NON_STR_KEYS coerces non-string dict keys (int, uuid, enum, ...)
        # to strings, matching stdlib json.dumps. Without it orjson raises
        # "Dict key must be str", turning e.g. a DRF ListField validation error
        # (keyed by integer item index) into a 500 instead of a clean 400.
        options = self.options | orjson.OPT_NON_STR_KEYS
        if media_type and self.html_media_type in media_type:
            options |= orjson.OPT_INDENT_2

        return orjson.dumps(data, default=_orjson_default, option=options)


class BrowsableAPIRenderer(renderers.BrowsableAPIRenderer):
    """
    HTML renderer used to self-document the API.

    This renderer populates version of the server running
    to the context.
    """

    def get_context(self, data, accepted_media_type, renderer_context):
        context = super().get_context(data, accepted_media_type, renderer_context)
        context["version"] = __version__
        return context


class PlainTextRenderer(renderers.BaseRenderer):
    media_type = "text/plain"
    format = "txt"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, dict):
            data = json.dumps(data)
        return data.encode(self.charset)
