from __future__ import unicode_literals

import sys
from typing import Callable, Any  # noqa: F401

from django.utils.translation import ugettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import APIException


class IncorrectStateException(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = _('Cannot modify an object in its current state.')


class RuntimeStateException(Exception):
    pass


class ExtensionDisabled(APIException):
    status_code = status.HTTP_424_FAILED_DEPENDENCY
    default_detail = _('Extension is disabled.')


def raise_with_traceback(exception, message="", *args, **kwargs):
    # type: (Callable, str, Any, Any) -> None
    """Raise exception with a specified traceback.

    This MUST be called inside a "except" clause.

    :param Exception exception: Error type to be raised.
    :param str message: Message to include with error, empty by default.
    :param args: Any additional args to be included with exception.
    """
    exc_type, exc_value, exc_traceback = sys.exc_info()
    # If not called inside a "except", exc_type will be None. Assume it will not happen
    exc_msg = "{}, {}: {}".format(message, exc_type.__name__, exc_value)  # type: ignore
    error = exception(exc_msg, *args, **kwargs)
    try:
        raise error.with_traceback(exc_traceback)
    except AttributeError:
        error.__traceback__ = exc_traceback
        raise error
